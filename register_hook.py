#!/usr/bin/env python3
"""注册 Copilot hook 到 ~/.workbuddy/settings.json

用法：
  python3 register_hook.py

会自动把 UserPromptSubmit + Stop 两个 hook 合并进 settings.json，
命令路径指向本项目的 copilot/hook.py。
"""
import json
import os
import shlex
import socket
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
HOOK_SCRIPT = PROJECT_ROOT / "copilot" / "hook.py"


def _settings_path() -> Path:
    """Honor WorkBuddy's explicit config root when a platform probe found it."""
    config_root = os.environ.get("WORKBUDDY_CONFIG_DIR")
    if config_root:
        return Path(config_root).expanduser() / "settings.json"
    return Path.home() / ".workbuddy" / "settings.json"


SETTINGS_PATH = _settings_path()

CFG_PATH = PROJECT_ROOT / "config.json"
cfg = {}
if CFG_PATH.exists():
    try:
        cfg = json.loads(CFG_PATH.read_text())
    except Exception:
        pass

student_id = (
    os.environ.get("COPILOT_STUDENT_ID")
    or cfg.get("student_id")
    or f"student-{socket.gethostname()}"
)
student_cfg = cfg.get("student", {})
if not isinstance(student_cfg, dict):
    student_cfg = {}
spool_dir = (
    os.environ.get("COPILOT_SPOOL_DIR")
    or student_cfg.get("spool_dir")
    or str(Path.home() / ".workbuddy" / "copilot" / "spool")
)

env_parts = [
    f"COPILOT_STUDENT_ID={shlex.quote(str(student_id))}",
    f"COPILOT_CONFIG={shlex.quote(str(CFG_PATH))}",
    f"COPILOT_SPOOL_DIR={shlex.quote(str(Path(spool_dir).expanduser().resolve()))}",
]
entry_owner = str(os.environ.get("COPILOT_ENTRY_OWNER") or "").strip()
if entry_owner:
    if not all(character.isalnum() or character in "_.-" for character in entry_owner):
        print("invalid COPILOT_ENTRY_OWNER", file=sys.stderr)
        raise SystemExit(2)
    env_parts.append(f"COPILOT_ENTRY_OWNER={shlex.quote(entry_owner)}")

verified_command = os.environ.get("COPILOT_HOOK_COMMAND")
reserved_hook_environment = (
    "COPILOT_STUDENT_ID",
    "COPILOT_CONFIG",
    "COPILOT_SPOOL_DIR",
    "COPILOT_ENTRY_OWNER",
)
if verified_command and any(
    name in verified_command for name in reserved_hook_environment
):
    print(
        "verified hook command must not override a reserved Copilot environment variable",
        file=sys.stderr,
    )
    raise SystemExit(2)
command_body = verified_command or " ".join(
    ["python3", shlex.quote(str(HOOK_SCRIPT)), "||", "true"]
)
hook_cmd = " ".join(env_parts + [command_body])

new_hooks = {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": hook_cmd, "timeout": 2}]}],
    "Stop": [{"hooks": [{"type": "command", "command": hook_cmd, "timeout": 2}]}],
}

# 读现有 settings
settings = {}
if SETTINGS_PATH.exists():
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
        print(f"已读取现有配置: {SETTINGS_PATH}")
    except json.JSONDecodeError:
        print(f"⚠️ {SETTINGS_PATH} 不是合法 JSON，将备份后重建")
        backup = SETTINGS_PATH.with_suffix(".json.bak")
        SETTINGS_PATH.rename(backup)
        print(f"已备份到 {backup}")
else:
    print(f"配置文件不存在，将创建: {SETTINGS_PATH}")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)

# 合并 hooks
existing_hooks = settings.get("hooks", {})
for event, blocks in new_hooks.items():
    if event not in existing_hooks:
        existing_hooks[event] = blocks
        print(f"  + 新增 {event} hook")
    else:
        # Upgrade an existing Copilot entry in place.  Leaving an old command
        # untouched would keep synchronous network/full-transcript behavior
        # after reinstall; unrelated user hooks remain unchanged.
        replacement = blocks[0]["hooks"][0]
        replaced = False
        for block in existing_hooks[event]:
            retained = []
            for hook in block.get("hooks", []):
                command = str(hook.get("command", ""))
                owned = bool(
                    entry_owner
                    and f"COPILOT_ENTRY_OWNER={entry_owner}" in command
                )
                # One-time migration of the pre-owner Copilot command.  Never
                # use this broad signature during uninstall; the protected
                # installer manifest and stable owner marker own that path.
                legacy = "copilot/hook.py" in command
                if owned or legacy:
                    if not replaced:
                        retained.append(dict(replacement))
                        replaced = True
                    continue
                retained.append(hook)
            block["hooks"] = retained
        if replaced:
            print(f"  ↻ {event} hook 已升级")
        else:
            existing_hooks[event].extend(blocks)
            print(f"  + 追加 {event} hook")

settings["hooks"] = existing_hooks


def _write_settings_atomically(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
    fd, raw_temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)

# 写回
_write_settings_atomically(SETTINGS_PATH, settings)
print(f"\n✅ 已写入 {SETTINGS_PATH}")
print(f"   hook 命令: {hook_cmd}")
print(f"\n下一步：在 WorkBuddy 中输入 /hooks 确认 hook 已启用")
