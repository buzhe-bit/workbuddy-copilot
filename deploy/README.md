# Ubuntu 单机发布

仅适用当前的 SQLite + 单 Uvicorn worker 架构。配置和数据在 release 目录外，更新只切换 `current` 软链接。

## 1. 首次准备

```bash
sudo adduser --system --group --home /var/lib/workbuddy-copilot workbuddy-copilot
sudo install -d -o root -g root /srv/workbuddy-copilot/releases
sudo install -d -m 0700 -o workbuddy-copilot -g workbuddy-copilot /srv/workbuddy-copilot/backups
sudo install -d -m 0700 -o workbuddy-copilot -g workbuddy-copilot /var/lib/workbuddy-copilot
sudo install -d -m 0750 -o root -g workbuddy-copilot /etc/workbuddy-copilot
sudo install -m 0640 -o root -g workbuddy-copilot config.example.json /etc/workbuddy-copilot/config.json
```

编辑外部配置：`store.db_path` 必须是 `/var/lib/workbuddy-copilot/copilot.db`，`auth.mode` 使用 `pilot`，每位学员使用独立 token。LLM 密钥只写入权限为 `0640 root:workbuddy-copilot` 的 `/etc/workbuddy-copilot/secrets.env`。

## 2. 从已验证 commit 创建 release

```bash
test -z "$(git status --porcelain)"
RELEASE="$(git rev-parse HEAD)"
DEST="/srv/workbuddy-copilot/releases/$RELEASE"
test ! -e "$DEST"
sudo install -d -o root -g root "$DEST"
git archive "$RELEASE" | sudo tar -x -C "$DEST"
sudo python3.13 -m venv "$DEST/venv"
sudo "$DEST/venv/bin/pip" install --requirement "$DEST/requirements-server.txt"
sudo chmod +x "$DEST/start_service.sh" "$DEST/deploy/preflight.sh"
sudo env COPILOT_CONFIG=/etc/workbuddy-copilot/config.json \
  PYTHON_BIN="$DEST/venv/bin/python" \
  WORKBUDDY_RELEASES_DIR=/srv/workbuddy-copilot/releases \
  "$DEST/deploy/preflight.sh"
```

## 3. 备份、切换与健康门禁

SQLite 在线备份使用 `sqlite3.Connection.backup`，可安全包含 WAL 中已提交内容：

```bash
PREVIOUS_RELEASE="$(readlink -f /srv/workbuddy-copilot/current || true)"
BACKUP="/srv/workbuddy-copilot/backups/copilot-$(date -u +%Y%m%dT%H%M%SZ)-$RELEASE.db"
if sudo test -f /var/lib/workbuddy-copilot/copilot.db; then
  sudo -u workbuddy-copilot python3.13 - /var/lib/workbuddy-copilot/copilot.db "$BACKUP" <<'PY'
import sqlite3, sys
import os
os.umask(0o077)
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
    assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
PY
fi

sudo systemctl stop workbuddy-copilot
sudo ln -sfn "$DEST" /srv/workbuddy-copilot/current.next
sudo mv -Tf /srv/workbuddy-copilot/current.next /srv/workbuddy-copilot/current
sudo systemctl start workbuddy-copilot

READY=
for _ in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8765/health >/dev/null; then READY=1; break; fi
  sleep 1
done
test "$READY" = 1
```

健康门禁失败时立即停止新版，恢复上一 release；若新版已执行迁移，在服务停止时用同样的 `Connection.backup` 将 `$BACKUP` 备份回写至数据库，不直接删 SQLite/WAL 文件：

```bash
sudo systemctl stop workbuddy-copilot
test -n "$PREVIOUS_RELEASE"
sudo ln -sfn "$PREVIOUS_RELEASE" /srv/workbuddy-copilot/current.next
sudo mv -Tf /srv/workbuddy-copilot/current.next /srv/workbuddy-copilot/current
sudo -u workbuddy-copilot python3.13 - "$BACKUP" /var/lib/workbuddy-copilot/copilot.db <<'PY'
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as source, sqlite3.connect(sys.argv[2]) as target:
    source.backup(target)
PY
sudo systemctl start workbuddy-copilot
```

## 4. systemd 与现有 Nginx 站点

```bash
sudo install -m 0644 "$DEST/deploy/workbuddy-copilot.service" /etc/systemd/system/workbuddy-copilot.service
sudo systemctl daemon-reload
sudo systemctl enable workbuddy-copilot
```

在现有 HTTPS `server {}` 中加入：

```nginx
include /srv/workbuddy-copilot/current/deploy/nginx-server.inc;
```

该 include 仅代理 Copilot 路由，不接管 `/`、`/developer-guide` 和 `/images`。最后执行：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail https://workbuddy-copilot.superbrain-ai.com/health
```
