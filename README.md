# WorkBuddy Copilot

面向 PLC 学习场景的实时学习辅助系统。学员端从 WorkBuddy hook 收集事件，经中心服务分析并把学习提示、导师消息回传给学员；导师在浏览器观察台查看学员状态和发送提示。

## 当前交付状态（2026-07-15）

| 范围 | 状态 | 说明 |
|---|---|---|
| 中心服务 | 已实现并自动验证 | `copilot.db` 是唯一权威源；服务端绝不读取学员机 WorkBuddy 文件、数据库或 JSONL。 |
| 共享学员核心 | 已实现并自动验证 | `Student Core` 负责本地 spool、HTTP/WS、重连、去重和消息回执；不依赖 macOS 或 Windows UI。 |
| macOS 学员端 | 已接入 | 既有 PyObjC `NSPanel` 浮标继续作为展示层，使用共享核心与显式 macOS 数据适配器；仍需按 P3 做实机冒烟。 |
| Windows 学员端 | 实现候选，**rollout blocked** | 共享核心、Windows WorkBuddy 适配、Tk 浮标、耐久消息/诊断、安装升级卸载和 Windows CI 已实现；缺真实 Windows WorkBuddy W0/W1 证据，不能宣称可上线。 |
| 反向导师消息 | 已实现并自动验证 | 消息先持久化，只有学员端成功 REST 回执后才标为已送达；断线、重启和响应丢失均有恢复路径。 |
| 导师干预雷达 | 已实现并自动验证 | 学习卡点与系统异常分类入队；导师可查看原对话、预填建议，或复制带证据和原文的 AI 审查包，所有学员消息仍需人工发送。 |

## 架构与数据边界

```text
WorkBuddy Hook (stdlib-only) ──本地原子 spool──> Student Core ──HTTPS/WSS──> Center Server
      │                                                        │                 │
      └─只读学员本机 transcript 尾部                             │                 └─copilot.db（唯一权威）
                                                               │
macOS: NSPanel + macOS adapter  <──────────── mentor message ──┘  <── Browser mentor desk
Windows: Tk UI + Windows adapter (implementation candidate; W0/W1 rollout blocked)
```

- Hook 是 stdlib-only、fire-and-forget：只读取受限的 transcript 尾部、原子写入本地 spool，任何异常都返回 0；它不联网，也不把本地路径发送给服务器。
- `Student Core` 是跨平台的常驻运行时。它从 `EventSpool` 发送事件，维护一条学生 WS，并在本地持久化“已渲染/已确认”的导师消息回执状态。
- 服务端接收上报并在自身 `copilot.db` 解析、入库与分析；不得访问 `~/.workbuddy`、学员数据库、JSONL 或任何学员文件系统。
- 应用必须以单个 uvicorn worker 运行：进程内 EventBus 和 WSRegistry 不能跨 worker 共享。

### 学员身份边界

- `local/demo` 可显式启用共享 student token，只用于单机开发和演示。
- `pilot/prod` 模式必须配置唯一的 `auth.student_tokens` 映射和独立 mentor token，共享 student token 会被强制关闭。
- 所有学员 REST/WS 路由均从 token 派生身份；客户端显式传入不一致的 `student_id` 会返回 403，不产生副作用。
- 这一边界已覆盖上报、历史、消息/回执、上传、提问/反馈和 WebSocket；学员 A token 不能读写学员 B 数据。

详细设计见 [目标架构](docs/target-architecture.md)、[PRD](docs/prd.md) 和 [测试方案 v3](docs/test-plan-v3.md)。

## 导师消息的送达语义

导师消息先写入 `mentor_messages`，`delivered_at` 保持为空。在线 WS 仅负责低延迟展示，不能直接改变送达状态。学员端成功处理消息后：

1. 先把“已渲染、待回执”持久化到本地；
2. 调用受 student token 保护的 `POST /api/student/messages/ack`；
3. 服务端成功持久化后才设置 `delivered_at` 并向导师端发布送达状态。

`GET /api/student/messages/pending-receipts` 只返回未确认消息，按 `id` 升序、最多 64 条，并支持 `after_id`。客户端会分页恢复；未知或未渲染消息绝不确认。该协议是 at-least-once 投递，而非把 WS 发送成功误当作送达。

## 运行

### 本地开发

```bash
python3.13 -m venv venv
venv/bin/python scripts/python_preflight.py
venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
./install.sh
./start_service.sh
./start_menubar.sh  # macOS 原生浮标 + 常驻 Hook spool 投递
```

release Python 合同由 [`pyproject.toml`](pyproject.toml) 固定为 `>=3.13,<3.14`，
[`.python-version`](.python-version) 提供 3.13 选择提示；测试与浏览器工具统一由
[`requirements-dev.txt`](requirements-dev.txt) 安装。

本地导师台地址为 `http://127.0.0.1:8765/mentor/`。`install.sh` 在 macOS 上安装 hook 并把 Hook spool 放在学员本机；它不把 WorkBuddy 数据位置配置给服务端。

### 公网部署

- 公网入口必须由 HTTPS/WSS 反向代理终止 TLS。
- 设置 `auth.mode` 为 `pilot` 或 `prod`，为每个学员生成不同的 `auth.student_tokens`；导师 token 不得下发到学员机。
- 以 `COPILOT_PUBLIC=1` 启动前确认 token 与 HTTPS/WSS 已就绪；应用保持单 worker。
- 示例：`COPILOT_PUBLIC=1 COPILOT_HOST=0.0.0.0 ./start_service.sh`。真实公网运行仍需要外部反向代理提供 TLS。
- 导师使用受保护的 `GET /api/mentor/system-status` 观察待分析、失败分析、未处理关注项和 WS 连接数。

Windows 不应执行 macOS 安装或浮标命令。安装、卸载、W0/W1 取证和回滚命令见 [试点运行手册](docs/pilot-runbook.md)。

## 主要接口

| 接口 | 身份 | 用途 |
|---|---|---|
| `POST /report` | student token | 接收 Hook/Student Core 事件；快速接受后由服务端后台处理。 |
| `GET /api/mentor/attention` | mentor token | 按状态、优先级、类别和学员查询干预队列。 |
| `PATCH /api/mentor/attention/{id}` | mentor token | 将关注项标为处理中、已解决或已忽略。 |
| `GET /api/mentor/system-status` | mentor token | 返回脱敏运行指标与 Windows rollout 门状态。 |
| `POST /api/mentor/message` | mentor token | 持久化并定向推送导师文字消息；可选 `client_request_id` 用于幂等恢复。 |
| `POST /api/mentor/messages/status` | mentor token | 按最多 300 个 `client_request_id` 补查持久化/展示状态，不返回消息正文。 |
| `GET /api/student/messages` | student token | 常规断线补拉。 |
| `GET /api/student/messages/pending-receipts` | student token | 仅取待确认导师消息；`limit` 最高 64，支持 `after_id`。 |
| `POST /api/student/messages/ack` | student token | 学员端成功渲染并持久化后的唯一“已展示”确认入口。 |
| `POST /api/student/asks/{id}/feedback` | student token | 提交 `helpful` 或 `unresolved`；未解决会进入导师关注队列。 |

## 测试

从仓库根目录运行：

```bash
PY=venv/bin/python
$PY scripts/python_preflight.py
$PY -m pytest tests/test_wb_upload.py tests/test_quality_summary.py -q
$PY -m pytest tests/test_platform_imports.py -q
$PY -m pytest tests/test_student_spool.py tests/test_student_transport.py tests/test_student_coordinator.py tests/test_student_agent.py tests/test_floating_native_phase3.py tests/test_e2e_reverse_message.py tests/test_message_service.py tests/test_mentor_api.py tests/e2e/test_student_agent_system.py -q
$PY -m pytest -q
git diff --check
```

当前通过数不在 README 手工维护；以 [Quality baseline 每次运行产生的
`quality-*` 机器可读摘要](https://github.com/SuperOPC-AI-Incubator/workbuddy-copilot/actions/workflows/quality.yml)
为准。自动测试不替代 P3 真实环境验证：macOS 原生 UI 仍需实机冒烟；Windows W0/W1
未完成前，Windows 发布门始终为 **BLOCKED**。

Linux server/core CI 对共享生产逻辑执行分支覆盖率门，`fail_under = 80`；原生平台 UI 由 macOS/Windows 独立平台门和真机门验证。

## 文档

- [目标架构设计](docs/target-architecture.md)
- [产品需求文档](docs/prd.md)
- [跨平台测试方案 v3](docs/test-plan-v3.md)
- [试点运行与回滚手册](docs/pilot-runbook.md)
- [质量基线与发布门](docs/quality-baseline.md)
- [WorkBuddy 本地文件结构调研（macOS 历史实测）](docs/workbuddy-file-structure.md)
- [开发与验证日志](docs/dev-log.md)

## License

MIT
