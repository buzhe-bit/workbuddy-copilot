# WorkBuddy Copilot

面向 PLC 学习场景的实时学习辅助系统。学员端从 WorkBuddy hook 收集事件，经中心服务分析并把学习提示、导师消息回传给学员；导师在浏览器观察台查看学员状态和发送提示。

## 当前交付状态（2026-07-12）

| 范围 | 状态 | 说明 |
|---|---|---|
| 中心服务 | 已实现并自动验证 | `copilot.db` 是唯一权威源；服务端绝不读取学员机 WorkBuddy 文件、数据库或 JSONL。 |
| 共享学员核心 | 已实现并自动验证 | `Student Core` 负责本地 spool、HTTP/WS、重连、去重和消息回执；不依赖 macOS 或 Windows UI。 |
| macOS 学员端 | 已接入 | 既有 PyObjC `NSPanel` 浮标继续作为展示层，使用共享核心与显式 macOS 数据适配器；仍需按 P3 做实机冒烟。 |
| Windows 学员端 | **BLOCKED** | 已有共享核心、Windows 适配器骨架和安装/导入合同；W0 真机证据与 W1 实机验收尚未完成，不能宣称支持或投产。 |
| 反向导师消息 | 已实现并自动验证 | 消息先持久化，只有学员端成功 REST 回执后才标为已送达；断线、重启和响应丢失均有恢复路径。 |

## 架构与数据边界

```text
WorkBuddy Hook (stdlib-only) ──本地原子 spool──> Student Core ──HTTPS/WSS──> Center Server
      │                                                        │                 │
      └─只读学员本机 transcript 尾部                             │                 └─copilot.db（唯一权威）
                                                               │
macOS: NSPanel + macOS adapter  <──────────── mentor message ──┘  <── Browser mentor desk
Windows: headless adapter skeleton (W0/W1 blocked)
```

- Hook 是 stdlib-only、fire-and-forget：只读取受限的 transcript 尾部、原子写入本地 spool，任何异常都返回 0；它不联网，也不把本地路径发送给服务器。
- `Student Core` 是跨平台的常驻运行时。它从 `EventSpool` 发送事件，维护一条学生 WS，并在本地持久化“已渲染/已确认”的导师消息回执状态。
- 服务端接收上报并在自身 `copilot.db` 解析、入库与分析；不得访问 `~/.workbuddy`、学员数据库、JSONL 或任何学员文件系统。
- 应用必须以单个 uvicorn worker 运行：进程内 EventBus 和 WSRegistry 不能跨 worker 共享。

### 当前 MVP 学员身份边界

- 当前所有学员共享的 `student_token` 只证明请求来自“学员端”，不能证明具体 `student_id`。
- HTTP 请求体、查询参数以及 `/ws` 的 `student_id` 都由客户端提供。持有共享 token 的客户端目前可冒充其他学员，读取或确认其消息，或以其身份连接 WebSocket。
- 按 `student_id` 存储和查询只能减少数据混写，不是授权隔离；当前部署不得称为学员级或租户级数据隔离。
- `auth.student_tokens` 与 `student_id_for_token()` 只是未接线路由的未来迁移接缝。只有路由从认证 principal 派生 `student_id`，并拒绝请求体、查询参数或 WS 中不匹配的值后，才能缓解上述风险。

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
venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
./install.sh
./start_service.sh
./start_menubar.sh  # 仅 macOS 原生浮标
```

项目 Python 版本由 [`.python-version`](.python-version) 固定为 3.13；测试与浏览器工具统一由
[`requirements-dev.txt`](requirements-dev.txt) 安装。

本地导师台地址为 `http://127.0.0.1:8765/mentor/`。`install.sh` 在 macOS 上安装 hook 并把 Hook spool 放在学员本机；它不把 WorkBuddy 数据位置配置给服务端。

### 公网部署

- 公网入口必须由 HTTPS/WSS 反向代理终止 TLS。
- 设置不同的 `COPILOT_STUDENT_TOKEN` 与 `COPILOT_MENTOR_TOKEN`；导师 token 不得下发到学员机。
- 以 `COPILOT_PUBLIC=1` 启动前确认 token 与 HTTPS/WSS 已就绪；应用保持单 worker。
- 示例：`COPILOT_PUBLIC=1 COPILOT_HOST=0.0.0.0 ./start_service.sh`。真实公网运行仍需要外部反向代理提供 TLS。

Windows 不应执行 macOS 安装或浮标命令。待真实 Windows WorkBuddy 完成 W0/W1 证据后，按 [测试方案 v3 的 Windows 门](docs/test-plan-v3.md#windows-实机门) 编写并验证其安装流程。

## 主要接口

| 接口 | 身份 | 用途 |
|---|---|---|
| `POST /report` | student token | 接收 Hook/Student Core 事件；快速接受后由服务端后台处理。 |
| `POST /api/mentor/message` | mentor token | 持久化并定向推送导师文字消息。 |
| `GET /api/student/messages` | student token | 常规断线补拉。 |
| `GET /api/student/messages/pending-receipts` | student token | 仅取待确认导师消息；`limit` 最高 64，支持 `after_id`。 |
| `POST /api/student/messages/ack` | student token | 学员端成功处理后的唯一送达确认入口。 |

## 测试

从仓库根目录运行：

```bash
PY=venv/bin/python
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

## 文档

- [目标架构设计](docs/target-architecture.md)
- [产品需求文档](docs/prd.md)
- [跨平台测试方案 v3](docs/test-plan-v3.md)
- [WorkBuddy 本地文件结构调研（macOS 历史实测）](docs/workbuddy-file-structure.md)
- [开发与验证日志](docs/dev-log.md)

## License

MIT
