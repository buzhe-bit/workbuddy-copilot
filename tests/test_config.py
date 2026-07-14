"""config.py 单元测试：配置加载、环境变量解析、路径展开。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import copilot.config as config_mod
from copilot.config import load_config, service_url, ws_url, _expand, _resolve_env


class TestExpand:
    def test_expand_user(self):
        assert _expand("~/test") == os.path.expanduser("~/test")

    def test_expand_dict(self):
        result = _expand({"path": "~/x", "name": "raw"})
        assert "~" not in result["path"]
        assert result["name"] == "raw"

    def test_expand_list(self):
        result = _expand(["~/a", "~/b", "plain"])
        assert "~" not in result[0]

    def test_passthrough_non_string(self):
        assert _expand(42) == 42
        assert _expand(True) is True


class TestResolveEnv:
    def test_api_key_env_resolved(self, monkeypatch):
        monkeypatch.setenv("TEST_API_KEY", "sk-secret")
        result = _resolve_env({"api_key_env": "TEST_API_KEY"})
        assert result["api_key"] == "sk-secret"

    def test_no_env_key(self):
        result = _resolve_env({"model": "gpt-4"})
        assert result["model"] == "gpt-4"


class TestLoadConfig:
    def test_load_valid_config(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "student_id": "test",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": "data/test.db"},
            "llm": {"api_base": "https://api.example.com/v1", "model": "test"},
        }))
        cfg = load_config(cfg_file)
        assert cfg["student_id"] == "test"
        assert cfg["llm"]["summary_model"] == "deepseek-v3-0324"
        assert cfg["service"]["analysis_max_concurrency"] == 2
        # 相对路径相对于 config 文件所在目录
        assert os.path.isabs(cfg["store"]["db_path"])

    def test_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.json")

    @pytest.mark.parametrize("invalid_value", [True, False, 1.0, 1.5, 0, -1])
    def test_analysis_max_concurrency_rejects_non_positive_integers(
        self, tmp_path, invalid_value,
    ):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "student_id": "test",
            "service": {
                "host": "127.0.0.1",
                "port": 8765,
                "analysis_max_concurrency": invalid_value,
            },
            "store": {"db_path": "data/test.db"},
            "llm": {},
        }))

        with pytest.raises(
            ValueError,
            match="service.analysis_max_concurrency must be a positive integer",
        ):
            load_config(cfg_file)

    def test_analysis_max_concurrency_preserves_explicit_positive_integer(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "student_id": "test",
            "service": {
                "host": "127.0.0.1",
                "port": 8765,
                "analysis_max_concurrency": 7,
            },
            "store": {"db_path": "data/test.db"},
            "llm": {},
        }))

        assert load_config(cfg_file)["service"]["analysis_max_concurrency"] == 7

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("local", True),
            ("demo", True),
            ("public", False),
            ("production", False),
            ("prod", False),
            ("pilot", False),
            ("staging", False),
            ("publci", False),
        ],
    )
    def test_shared_student_token_default_follows_deployment_mode(
        self,
        tmp_path,
        mode,
        expected,
    ):
        cfg_file = tmp_path / f"{mode}.json"
        cfg_file.write_text(json.dumps({
            "student_id": "test",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": "data/test.db"},
            "auth": {"mode": mode},
            "llm": {},
        }))

        assert load_config(cfg_file)["auth"]["allow_shared_student_token"] is expected

    def test_explicit_local_shared_student_token_disable_is_preserved(self, tmp_path):
        cfg_file = tmp_path / "local-disabled.json"
        cfg_file.write_text(json.dumps({
            "student_id": "test",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": "data/test.db"},
            "auth": {"mode": "local", "allow_shared_student_token": False},
            "llm": {},
        }))

        assert load_config(cfg_file)["auth"]["allow_shared_student_token"] is False

    @pytest.mark.parametrize("mode", ["public", "pilot", "staging", "publci"])
    def test_non_local_shared_student_token_opt_in_is_forced_closed(
        self,
        tmp_path,
        mode,
    ):
        cfg_file = tmp_path / f"{mode}-opt-in.json"
        cfg_file.write_text(json.dumps({
            "student_id": "test",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": "data/test.db"},
            "auth": {"mode": mode, "allow_shared_student_token": True},
            "llm": {},
        }))

        assert load_config(cfg_file)["auth"]["allow_shared_student_token"] is False

    def test_default_config_falls_back_to_example_when_config_json_missing(
        self, tmp_path, monkeypatch, caplog,
    ):
        monkeypatch.delenv("COPILOT_CONFIG", raising=False)
        missing_default = tmp_path / "config.json"
        example = tmp_path / "config.example.json"
        example.write_text(json.dumps({
            "student_id": "example-student",
            "service": {"host": "127.0.0.1", "port": 8765},
            "store": {"db_path": "data/example.db"},
            "llm": {"api_base": "https://api.example.com/v1", "model": "test"},
        }), encoding="utf-8")
        monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", missing_default)
        monkeypatch.setattr(config_mod, "EXAMPLE_CONFIG_PATH", example, raising=False)
        caplog.set_level("WARNING", logger="copilot.config")

        cfg = load_config()

        assert cfg["student_id"] == "example-student"
        assert cfg["store"]["db_path"] == str(tmp_path / "data" / "example.db")
        assert "config.example.json" in caplog.text


class TestUrlBuilders:
    def test_service_url_base(self):
        cfg = {"service": {"host": "127.0.0.1", "port": 8765}}
        assert service_url(cfg) == "http://127.0.0.1:8765"

    def test_service_url_with_path(self):
        cfg = {"service": {"host": "127.0.0.1", "port": 8765}}
        assert service_url(cfg, "/health") == "http://127.0.0.1:8765/health"

    def test_ws_url(self):
        cfg = {"service": {"host": "127.0.0.1", "port": 8765}}
        assert ws_url(cfg) == "ws://127.0.0.1:8765/ws"

    def test_public_base_url_overrides_host_port_for_https_and_wss(self):
        cfg = {
            "service": {
                "host": "127.0.0.1",
                "port": 8765,
                "public_base_url": "https://copilot.example.com/copilot/",
            }
        }

        assert service_url(cfg) == "https://copilot.example.com/copilot"
        assert service_url(cfg, "/health") == "https://copilot.example.com/copilot/health"
        assert ws_url(cfg) == "wss://copilot.example.com/copilot/ws"

    def test_public_base_url_uses_ws_for_http_base(self):
        cfg = {"service": {"public_base_url": "http://localhost:8765"}}

        assert service_url(cfg, "/report") == "http://localhost:8765/report"
        assert ws_url(cfg, "/ws/mentor") == "ws://localhost:8765/ws/mentor"
