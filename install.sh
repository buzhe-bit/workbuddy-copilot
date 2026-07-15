#!/usr/bin/env bash
# Install the macOS student client, protect its token-bearing config, and
# register only manifest-owned WorkBuddy hooks.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON="${PYTHON:-python3.13}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "BLOCKED: 找不到 Python 3.13，请先安装 python3.13。" >&2
  exit 1
fi
"$PYTHON" "$PROJECT_DIR/scripts/python_preflight.py"

if pgrep -x "WorkBuddy" >/dev/null 2>&1; then
  echo "BLOCKED: WorkBuddy 正在运行；请完全退出后再安装。" >&2
  exit 1
fi

CONFIG_PATH="$PROJECT_DIR/config.json"
if [[ ! -f "$CONFIG_PATH" ]]; then
  umask 077
  cp "$PROJECT_DIR/config.example.json" "$CONFIG_PATH"
  chmod 600 "$CONFIG_PATH"
  echo "已创建私有配置 $CONFIG_PATH"
  echo "请填写 student_id、service.public_base_url、auth.mode=pilot 和本学员 auth.student_token；保持 mentor_token、student_tokens、llm.api_key 为空，然后重新运行 ./install.sh。"
  exit 2
fi
chmod 600 "$CONFIG_PATH"

WORKBUDDY_DIR="$HOME/.workbuddy"
SETTINGS_PATH="$WORKBUDDY_DIR/settings.json"
HOOK_LINK="$WORKBUDDY_DIR/copilot/hook.py"
HOOK_TARGET="$PROJECT_DIR/copilot/hook.py"
COPILOT_SPOOL_DIR="$WORKBUDDY_DIR/copilot/spool"
STATE_DIR="$HOME/.workbuddy-copilot"
STATE_HELPER="$PROJECT_DIR/scripts/macos_install_state.py"
OWNER_ID="workbuddy-copilot-macos-v1"

TRANSACTION="$("$PYTHON" "$STATE_HELPER" prepare \
  --project-root "$PROJECT_DIR" \
  --config "$CONFIG_PATH" \
  --workbuddy-root "$WORKBUDDY_DIR" \
  --settings "$SETTINGS_PATH" \
  --hook-link "$HOOK_LINK" \
  --spool-dir "$COPILOT_SPOOL_DIR" \
  --state-dir "$STATE_DIR")"

rollback_install() {
  local exit_code=$?
  trap - ERR INT TERM
  if [[ -n "${TRANSACTION:-}" && -f "$TRANSACTION" ]]; then
    local rollback_output
    if ! rollback_output="$("$PYTHON" "$STATE_HELPER" rollback \
      --state-dir "$STATE_DIR" \
      --workbuddy-root "$WORKBUDDY_DIR" 2>&1)"; then
      echo "ROLLBACK FAILED: $rollback_output" >&2
      exit 70
    fi
  fi
  if [[ "$exit_code" -eq 0 ]]; then
    exit_code=1
  fi
  exit "$exit_code"
}
trap rollback_install ERR INT TERM

echo "==> 创建 Python 3.13 学员端环境"
"$PYTHON" -m venv "$PROJECT_DIR/venv"
VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
"$VENV_PYTHON" "$PROJECT_DIR/scripts/python_preflight.py"
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$PROJECT_DIR/requirements-macos.txt"

mkdir -p "$WORKBUDDY_DIR/copilot" "$COPILOT_SPOOL_DIR"
chmod 700 "$WORKBUDDY_DIR/copilot" "$COPILOT_SPOOL_DIR"
ln -s "$HOOK_TARGET" "$HOOK_LINK"

printf -v COPILOT_HOOK_COMMAND '%q %q || true' "$VENV_PYTHON" "$HOOK_TARGET"
export WORKBUDDY_CONFIG_DIR="$WORKBUDDY_DIR"
export COPILOT_STUDENT_ID
COPILOT_STUDENT_ID="$("$VENV_PYTHON" -c \
  'import sys; from copilot.config import load_config; print(load_config(sys.argv[1])["student_id"])' \
  "$CONFIG_PATH")"
export COPILOT_SPOOL_DIR COPILOT_HOOK_COMMAND
export COPILOT_ENTRY_OWNER="$OWNER_ID"
"$VENV_PYTHON" "$PROJECT_DIR/register_hook.py"

MANIFEST="$("$PYTHON" "$STATE_HELPER" finalize \
  --state-dir "$STATE_DIR" \
  --workbuddy-root "$WORKBUDDY_DIR")"
trap - ERR INT TERM

echo ""
echo "macOS 学员端安装完成。"
echo "安装清单: $MANIFEST"
echo "启动: ./start_menubar.sh"
echo "卸载/回滚: ./uninstall_macos.sh"
echo "首次启动浮标时，按系统提示授予必要的辅助功能权限。"
