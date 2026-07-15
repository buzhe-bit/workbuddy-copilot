#!/usr/bin/env bash
# 启动后台分析服务
set -e
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

for worker_var in COPILOT_WORKERS UVICORN_WORKERS WEB_CONCURRENCY; do
  worker_value="${!worker_var}"
  if [ -n "$worker_value" ] && [ "$worker_value" != "1" ]; then
    echo "错误: $worker_var 必须为空或恰好为 1" >&2
    exit 1
  fi
done

# 从 API Vault 加载凭证（含 TENCENT_TOKENHUB_API_KEY）
VAULT_FILE="$HOME/.claude/api-vault.env"
if [ -f "$VAULT_FILE" ]; then
  set -a
  source "$VAULT_FILE"
  set +a
  echo "已加载 API Vault: $VAULT_FILE"
else
  echo "⚠️  未找到 $VAULT_FILE，LLM 分析将走降级路径"
fi

source venv/bin/activate

COPILOT_HOST="${COPILOT_HOST:-127.0.0.1}"
COPILOT_PORT="${COPILOT_PORT:-8765}"

echo "启动 Copilot 分析服务 http://$COPILOT_HOST:$COPILOT_PORT"
echo "  LLM: $(python3 -c 'from copilot.config import load_config; c=load_config(); print(c["llm"]["provider"], c["llm"]["model"])')"
if [ "${COPILOT_PUBLIC:-}" = "1" ]; then
  echo "  公网模式: 请确认已设置 COPILOT_STUDENT_TOKEN 和 COPILOT_MENTOR_TOKEN，并在反向代理上启用 HTTPS/WSS"
fi
echo "按 Ctrl+C 退出"
echo ""

exec uvicorn copilot.service:app \
  --host "$COPILOT_HOST" \
  --port "$COPILOT_PORT" \
  --workers "1" \
  --log-level info
