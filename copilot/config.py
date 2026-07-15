"""配置加载：默认从项目根的 config.json 读，环境变量可覆盖。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
EXAMPLE_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.example.json"

log = logging.getLogger("copilot.config")


def _validate_analysis_max_concurrency(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(
            "service.analysis_max_concurrency must be a positive integer"
        )
    return value


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expanduser(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _resolve_env(value: Any) -> Any:
    """支持 {api_key_env: DEEPSEEK_API_KEY} 形式：从环境变量取真实值。"""
    if isinstance(value, dict):
        if "api_key_env" in value:
            env_name = value["api_key_env"]
            value = {**value, "api_key": os.environ.get(env_name, "")}
        return {k: _resolve_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v) for v in value]
    return value


def load_config(path: Path | str | None = None) -> dict:
    environment_path = os.environ.get("COPILOT_CONFIG")
    explicit_path = path is not None or bool(environment_path)
    cfg_path = Path(path or environment_path) if explicit_path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if not explicit_path and EXAMPLE_CONFIG_PATH.exists():
            log.warning(
                "config.json not found at %s; falling back to %s",
                cfg_path,
                EXAMPLE_CONFIG_PATH,
            )
            cfg_path = EXAMPLE_CONFIG_PATH
        else:
            raise FileNotFoundError(
                f"找不到配置文件 {cfg_path}。请复制 config.example.json 为 config.json 并填写。"
            )
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    cfg = _resolve_env(_expand(raw))
    cfg.setdefault("llm", {}).setdefault("summary_model", "deepseek-v3-0324")
    auth_cfg = cfg.setdefault("auth", {})
    auth_mode = str(auth_cfg.get("mode", "") or "").lower()
    if auth_mode in {"", "local", "demo"}:
        auth_cfg.setdefault("allow_shared_student_token", True)
    else:
        auth_cfg["allow_shared_student_token"] = False
    service_cfg = cfg.setdefault("service", {})
    configured_concurrency = service_cfg.setdefault("analysis_max_concurrency", 2)
    service_cfg["analysis_max_concurrency"] = _validate_analysis_max_concurrency(
        configured_concurrency
    )
    # 相对路径相对于项目根目录解析
    project_root = cfg_path.resolve().parent
    db_path = cfg.get("store", {}).get("db_path", "")
    if db_path and not os.path.isabs(db_path):
        cfg["store"]["db_path"] = str(project_root / db_path)
    windows_rollout = cfg.get("windows_rollout")
    if isinstance(windows_rollout, dict):
        evidence_path = str(windows_rollout.get("evidence_path") or "").strip()
        if evidence_path and not os.path.isabs(evidence_path):
            windows_rollout["evidence_path"] = str(project_root / evidence_path)
    return cfg


def service_url(cfg: dict, path: str = "") -> str:
    svc = cfg["service"]
    base = str(svc.get("public_base_url") or "").strip().rstrip("/")
    if not base:
        base = f"http://{svc['host']}:{svc['port']}"
    return base + path if path else base


def ws_url(cfg: dict, path: str = "/ws") -> str:
    svc = cfg["service"]
    base = str(svc.get("public_base_url") or "").strip().rstrip("/")
    if base:
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + path
    return f"ws://{svc['host']}:{svc['port']}{path}"
