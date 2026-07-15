#!/usr/bin/env bash
# 启动桌面浮标 + 只投递 Hook spool 的 Student Core
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

source venv/bin/activate

CORE_PID=""
UI_PID=""
cleanup() {
  status=$?
  trap - EXIT INT TERM
  for pid in "$UI_PID" "$CORE_PID"; do
    if [[ -n "$pid" ]]; then
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    fi
  done
  exit "$status"
}
trap cleanup EXIT INT TERM

python3 "$PROJECT_DIR/start_student_agent.py" \
  --config "$PROJECT_DIR/config.json" \
  --spool-only &
CORE_PID=$!

echo "启动 Copilot 浮动浮标..."
echo "（圆形可拖拽图标会出现在桌面上）"
echo ""

python3 -m copilot.floating_native &
UI_PID=$!
wait "$UI_PID"
