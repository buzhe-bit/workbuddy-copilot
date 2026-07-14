from __future__ import annotations

import json
import signal
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import copilot.app_context as app_context
from copilot.app_context import assert_single_worker, build_context, token_is_valid
from copilot.service import create_app


def _write_config(tmp_path):
    cfg = {
        "student_id": "student-test",
        "service": {"host": "127.0.0.1", "port": 8765},
        "llm": {
            "provider": "test",
            "api_base": "http://example.invalid",
            "model": "test",
            "api_key": "",
        },
        "store": {"db_path": str(tmp_path / "copilot.db")},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def test_build_context_constructs_dependencies(tmp_path, monkeypatch):
    monkeypatch.delenv("COPILOT_WORKERS", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)

    context = build_context(_write_config(tmp_path))

    assert context.config["student_id"] == "student-test"
    assert context.store is not None
    assert context.analysis_svc is not None
    assert context.session_svc is not None
    assert context.bus is not None
    assert context.ws_registry is not None


def test_assert_single_worker_rejects_multiple_workers(monkeypatch):
    monkeypatch.setenv("COPILOT_WORKERS", "2")
    with pytest.raises(RuntimeError):
        assert_single_worker()


@pytest.mark.parametrize(
    "argv",
    [
        [
            "/venv/lib/python3.13/site-packages/uvicorn/__main__.py",
            "copilot.service:app",
            "--workers",
            "2",
        ],
        ["/venv/bin/uvicorn", "copilot.service:app", "--workers=2"],
    ],
)
def test_assert_single_worker_rejects_uvicorn_cli_workers(monkeypatch, argv):
    monkeypatch.delenv("COPILOT_WORKERS", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(RuntimeError, match=r"--workers(?:=| )2"):
        assert_single_worker()


def test_assert_single_worker_ignores_unrelated_workers_argument(monkeypatch):
    monkeypatch.delenv("COPILOT_WORKERS", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(sys, "argv", ["manage.py", "job", "--workers", "2"])

    assert_single_worker()


def test_assert_single_worker_allows_uvicorn_cli_one_worker(monkeypatch):
    monkeypatch.delenv("COPILOT_WORKERS", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["/venv/bin/uvicorn", "copilot.service:app", "--workers=1"],
    )

    assert_single_worker()


def test_assert_single_worker_allows_one_worker(monkeypatch):
    monkeypatch.setenv("COPILOT_WORKERS", "1")
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    assert_single_worker()


def test_token_check_uses_constant_time_compare(monkeypatch):
    calls = []

    def fake_compare(supplied, expected):
        calls.append((supplied, expected))
        return True

    monkeypatch.setattr(app_context.hmac, "compare_digest", fake_compare)

    assert token_is_valid({"auth": {"token": "secret-token"}}, "wrong-token") is True
    assert calls == [("wrong-token", "secret-token")]


def test_token_check_stays_open_when_no_token_configured(monkeypatch):
    def fail_compare(*args):
        raise AssertionError("open dev mode should not compare tokens")

    monkeypatch.setattr(app_context.hmac, "compare_digest", fail_compare)

    assert token_is_valid({}, None) is True


def test_student_token_mapping_resolves_exact_student():
    config = {
        "auth": {
            "student_tokens": {
                "student-a": "token-a",
                "student-b": "token-b",
            }
        }
    }

    assert app_context.student_id_for_token(config, "token-b") == "student-b"


def test_student_token_mapping_compares_non_ascii_string_tokens():
    config = {"auth": {"student_tokens": {"student-a": "学员令牌"}}}

    assert app_context.student_id_for_token(config, "学员令牌") == "student-a"


@pytest.mark.parametrize(
    ("config", "supplied_token"),
    [
        ({}, "token-a"),
        ({"auth": {}}, "token-a"),
        ({"auth": {"student_tokens": {"student-a": "token-a"}}}, "unknown"),
        ({"auth": {"student_tokens": {"student-a": "token-a"}}}, ""),
        ({"auth": {"student_tokens": {"student-a": "token-a"}}}, None),
        ({"auth": {"student_tokens": []}}, "token-a"),
        ({"auth": {"student_tokens": {"student-a": ""}}}, "token-a"),
        ({"auth": {"student_tokens": {"student-a": 123}}}, "token-a"),
        ({"auth": {"student_tokens": {"": "token-a"}}}, "token-a"),
    ],
)
def test_student_token_mapping_fails_closed_for_missing_or_invalid_values(
    config,
    supplied_token,
):
    assert app_context.student_id_for_token(config, supplied_token) is None


def test_student_token_mapping_rejects_duplicate_token_ambiguity():
    config = {
        "auth": {
            "student_tokens": {
                "student-a": "duplicate-token",
                "student-b": "duplicate-token",
            }
        }
    }

    assert app_context.student_id_for_token(config, "duplicate-token") is None


def test_student_token_mapping_authenticates_mapped_and_local_shared_tokens():
    config = {
        "auth": {
            "student_token": "shared-token",
            "student_tokens": {"student-a": "student-a-token"},
        }
    }

    assert token_is_valid(config, "shared-token", role="student") is True
    assert token_is_valid(config, "student-a-token", role="student") is True
    assert app_context.student_id_for_token(config, "shared-token") is None


def test_configured_student_mapping_closes_tokenless_local_fallback():
    config = {
        "student_id": "server-default",
        "auth": {
            "mode": "local",
            "student_tokens": {"student-a": "student-a-token"},
        },
    }

    assert token_is_valid(config, None, role="student") is False
    assert token_is_valid(config, "unknown", role="student") is False
    principal = app_context.student_principal_for_token(config, "student-a-token")
    assert principal == app_context.StudentPrincipal("student-a", "mapped")


def test_lifespan_rejects_second_process_for_same_db(tmp_path, monkeypatch):
    monkeypatch.delenv("COPILOT_WORKERS", raising=False)
    monkeypatch.delenv("UVICORN_WORKERS", raising=False)
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    cfg_path = _write_config(tmp_path)
    first = build_context(cfg_path)
    second = build_context(cfg_path)

    with TestClient(create_app(first)):
        with pytest.raises(RuntimeError, match="single uvicorn worker"):
            with TestClient(create_app(second)):
                pass

    with TestClient(create_app(second)):
        pass


def test_lock_collision_signals_only_uvicorn_multiprocess_supervisor(
    tmp_path,
    monkeypatch,
):
    cfg_path = _write_config(tmp_path)
    first = build_context(cfg_path)
    second = build_context(cfg_path)
    supervisor = SimpleNamespace(pid=4242)
    signals = []
    monkeypatch.setattr(
        sys,
        "argv",
        ["/venv/bin/uvicorn", "copilot.service:app", "--workers", "2"],
    )
    monkeypatch.setattr(
        app_context.multiprocessing,
        "parent_process",
        lambda: supervisor,
    )
    monkeypatch.setattr(
        app_context.os,
        "kill",
        lambda pid, sig: signals.append((pid, sig)),
    )

    with TestClient(create_app(first)):
        with pytest.raises(RuntimeError, match="single uvicorn worker"):
            with TestClient(create_app(second)):
                pass

    assert signals == [(supervisor.pid, signal.SIGTERM)]


def test_lock_collision_does_not_signal_unrelated_multiprocess_parent(
    tmp_path,
    monkeypatch,
):
    cfg_path = _write_config(tmp_path)
    first = build_context(cfg_path)
    second = build_context(cfg_path)
    monkeypatch.setattr(sys, "argv", ["manage.py", "job", "--workers", "2"])
    monkeypatch.setattr(
        app_context.multiprocessing,
        "parent_process",
        lambda: SimpleNamespace(pid=4242),
    )
    monkeypatch.setattr(
        app_context.os,
        "kill",
        lambda *_: pytest.fail("unrelated parent must not be signaled"),
    )

    with TestClient(create_app(first)):
        with pytest.raises(RuntimeError, match="single uvicorn worker"):
            with TestClient(create_app(second)):
                pass
