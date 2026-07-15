#!/usr/bin/env bash
# Remove only the macOS hooks and link owned by this release manifest.
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_HELPER="$PROJECT_DIR/scripts/macos_install_state.py"
WORKBUDDY_DIR="$HOME/.workbuddy"
STATE_DIR="$HOME/.workbuddy-copilot"

if [[ $# -ne 0 ]]; then
  echo "用法: ./uninstall_macos.sh" >&2
  exit 2
fi

if ! WORKBUDDY_RUNNING="$(osascript -e 'application "WorkBuddy" is running' 2>/dev/null)"; then
  echo "BLOCKED: WorkBuddy 运行状态探测失败；已按安全默认停止卸载。" >&2
  exit 1
fi
case "$WORKBUDDY_RUNNING" in
  true)
    echo "BLOCKED: WorkBuddy 正在运行；请完全退出后再卸载。" >&2
    exit 1
    ;;
  false) ;;
  *)
    echo "BLOCKED: WorkBuddy 运行状态返回未知结果；已按安全默认停止卸载。" >&2
    exit 1
    ;;
esac

if [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
  PYTHON="$PROJECT_DIR/venv/bin/python"
else
  PYTHON="${PYTHON:-python3.13}"
fi
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "BLOCKED: 找不到此 release 的 Python 3.13。" >&2
  exit 1
fi
"$PYTHON" "$PROJECT_DIR/scripts/python_preflight.py"
"$PYTHON" "$STATE_HELPER" uninstall \
  --state-dir "$STATE_DIR" \
  --workbuddy-root "$WORKBUDDY_DIR" \
  --project-root "$PROJECT_DIR" >/dev/null

echo "已移除当前 release 拥有的 WorkBuddy hooks 与 hook 链接。"
echo "config.json、venv、spool、日志和私有备份仍保留，确认不再需要后再人工处理。"
