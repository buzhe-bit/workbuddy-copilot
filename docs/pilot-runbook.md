# WorkBuddy Copilot 试点运行与回滚手册

## 1. 发布口径

- 目标容量是 50 名学员、25 名导师；先用 3–5 名学员、1–2 名导师运行 7 天。
- Linux 中心服务、共享 Student Core 和导师台是可验证的实现候选。
- Windows 已有可安装客户端候选，但真实 WorkBuddy 的 W0/W1 证据未完成前始终是 `rollout blocked`。
- macOS 原生浮标也需完成安装、休眠唤醒、断网恢复和消息回执实机冒烟。
- 本试点只面向已知情的成年内部学员。正式营期前仍需账号可见范围、隐私同意、自动留存策略和审计方案。

## 2. 中心服务配置

发布用 `config.json` 至少满足：

```json
{
  "service": {
    "host": "127.0.0.1",
    "port": 8765,
    "public_base_url": "https://copilot.example.com",
    "analysis_max_concurrency": 4
  },
  "auth": {
    "mode": "pilot",
    "allow_shared_student_token": false,
    "student_tokens": {
      "student-001": "<unique-random-token-001>",
      "student-002": "<unique-random-token-002>"
    },
    "mentor_token": "<independent-mentor-token>"
  },
  "store": {
    "db_path": "/var/lib/workbuddy-copilot/copilot.db"
  }
}
```

- token 不提交到 Git；配置文件只允许服务账号读取。每个学员 token 必须唯一，导师 token 不下发到学员机。
- `analysis_max_concurrency=4` 是 50 人目标的起始值，不是已证明的最优值。只能根据真实 LLM 限流、延迟和待处理积压单变量调整。
- 公网只暴露 TLS 反向代理；Uvicorn 固定单 worker，不得设置 `WEB_CONCURRENCY>1`。
- 启动后先查 `GET /health`，再携导师 token 查 `GET /api/mentor/system-status`。

## 3. 发布前清单

1. 记录 commit SHA、Python 3.13.x、WorkBuddy/macOS/Windows 版本与 Windows CPU 架构。
2. 三条 CI 线全绿：Linux server/core、macOS client/browser、Windows hosted runtime；critical skip 为 0。
3. Linux 共享生产逻辑分支覆盖率至少 80%；60 条诊断集指标达标。
4. 备份旧 `copilot.db` 和旧配置，并验证 SQLite `PRAGMA integrity_check` 返回 `ok`。
5. 用学员 A token 实测读取、确认或 WS 连接学员 B，必须为 403 且无副作用。
6. 三个导师台 viewport（1440×900、700×570、390×844）无横向溢出，消息输入可用。
7. Windows 只有在 W0/W1 证据与当前 commit/build 匹配、validator 返回 rollout ready 后才可进入试点。

## 4. 运行中观察与处理

导师台的系统状态是第一入口，服务日志和数据库是追查证据。

| 指标 | 正常口径 | 处理动作 |
|---|---|---|
| `pending_analyses` | 短时波动后能持续下降 | 连续 5 分钟不下降或超过 25：暂停扩大试点，查 LLM 限流/超时和单 worker 日志。 |
| `failed_analyses` | 不应持续增长 | 该值是当前失败数；发现增量即查“系统异常”关注项和错误码，人工重试前先保留证据。 |
| `open_attention` | 导师班次内有人接管 | high 等待超过 30 秒先确认学员已被看到；不经判断不自动发送 AI 建议。 |
| `float_connections` | 等于当前应在线学员端数 | 单个学员离线先查本地 heartbeat/log；集体下降查 TLS、服务进程和反代。 |
| `mentor_connections` | 等于当前打开导师台的活跃连接 | 为 0 不代表学习事件丢失；导师重连后通过 REST 补拉队列。 |

每日至少记录：最大待分析数、失败增量、high 关注最长等待、断网/重启恢复结果、跨学员数据事故数。任何跨学员数据泄漏、静默丢事件或未发现的持续积压都立即停止试点。

## 5. macOS 学员端

安装前由运营方为每位学员准备唯一 `student_id`、HTTPS `service.public_base_url` 和对应的唯一 student token。学员机 `config.json` 设置 `auth.mode=pilot`、`allow_shared_student_token=false`，只填写本学员的 `auth.student_token`；`mentor_token`、`student_tokens`、旧 `auth.token` 和 `llm.api_key` 必须为空。

```bash
cp config.example.json config.json
chmod 600 config.json
# 填写上面的学员字段，不写导师 token 或 LLM key
PYTHON=python3.13 ./install.sh
./start_menubar.sh
```

安装器强制 Python 3.13，只安装 macOS 学员端依赖，并在改 WorkBuddy settings 前将去除旧 Copilot hooks 的基线原子写入 `~/.workbuddy-copilot/`；config、备份和 manifest 仅当前用户可读。实机必验：Hook 事件上报、浮标展示一次、导师消息渲染后才送达、断网恢复、进程重启、休眠唤醒。

回滚前先停止学员端，再运行：

```bash
./uninstall_macos.sh
```

卸载器只处理 manifest 拥有的 hooks 和 hook 链接；若 WorkBuddy settings 在安装后被用户修改，只移除当前 owner 条目而不覆盖其他改动。为避免误删学员证据，`config.json`、venv、spool、日志和私有备份默认保留。

## 6. Windows 学员端

Windows 命令必须在受信任、无未审查改动的仓库副本中运行。先用 `probe_windows_workbuddy.ps1` 生成脱敏 W0 事实，再由人工确认 profile；安装器不猜 WorkBuddy 路径。

```powershell
py -3.13 -m venv .venv-windows
.\.venv-windows\Scripts\python.exe -m pip install -r requirements-core.txt -r requirements-windows.txt
.\probe_windows_workbuddy.ps1 -BuildId <build> -CommitSha <40-char-sha> -ProfilePath <verified-profile.json>
.\install_windows.ps1 `
  -ProjectRoot <absolute-repo> `
  -ConfigDir <verified-workbuddy-config-dir> `
  -ProfilePath <verified-profile.json> `
  -StudentId student-001 `
  -GitBashHookCommand <verified-git-bash-hook-command> `
  -BaseUrl https://copilot.example.com `
  -TokenFile <private-token-file> `
  -StateDir <private-state-dir> `
  -LogDir <private-state-dir\logs>
```

安装完记录 `<private-state-dir>\installer-manifest.json`，执行 `py -3.13 start_windows_client.py --config <private-state-dir>\client-config.json --health-check`。W1 必须在受信任真机/自托管 runner 上用 `run_windows_w1.ps1` 完成；hosted Windows 绿灯只代表 implementation candidate。

卸载/回滚：

```powershell
.\uninstall_windows.ps1 -ManifestPath <private-state-dir\installer-manifest.json>
```

卸载器只按 manifest 删除属于 Copilot 的 hook、计划任务和运行环境；只有 WorkBuddy settings hash 未改变时才恢复整份基线备份。失败时保留 manifest、lifecycle log 和日志，不手工删 claim 或改 token 目录 ACL。

## 7. 中心数据备份、恢复与删除

1. 停止中心服务，确认单 worker 已退出。
2. 将 `copilot.db`、发布配置的脱敏副本、commit/build 和 Windows evidence hash 复制到受限备份目录。
3. 用 `PRAGMA integrity_check` 验证备份，再重启服务。
4. 恢复时保持服务停止，先备份当前库，替换为已验证备份，启动后查 `/health`、`system-status` 和幂等重放。
5. 删除单个试点学员数据时，使用导师鉴权的 `DELETE /api/admin/students/{student_id}`，核对返回的各表删除计数；不直接手改 SQLite 表。

正式自动 retention 尚不在本试点交付内。在其实现前，每次试点必须记录人工删除日期和责任人。

## 8. 紧急停止条件

任一条发生即停止新事件接入，保留 DB/日志/版本证据，回滚至上一已验证版本：

- 跨学员读写或身份冒充；
- `/report` 已 202 但重启后无法恢复；
- 导师消息未渲染却被标为已送达；
- 持续积压未被系统状态或关注队列暴露；
- Windows evidence 与当前 commit/build 不一致；
- 安装/卸载覆盖了用户后续新增的 WorkBuddy hooks。
