# WorkBuddy Copilot 原闭环加固与导师干预雷达 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 Controller–Service–Repository、EventBus、Student Core、本地 spool、SQLite、单 worker 和静态导师台架构的前提下，建立可复现质量门、加固原闭环，并交付 AI 优先解题、导师干预雷达、小范围试点保障、百人级容量证据与可复现实验式成本优化。

**Architecture:** 服务端继续以 FastAPI 路由调用 Service，Service 通过 Store 持久化并经 EventBus/WSRegistry 推送。Hook 仍 stdlib-only，Student Core 仍负责 spool/HTTP/WS/回执。新增 attention 是已持久化 analysis/student_ask/system 结果的可重建投影，不引入 broker、Redis、多 worker 或前端框架。

**Tech Stack:** Python 3.13, FastAPI, SQLite, asyncio/websockets, stdlib Hook, PyObjC macOS adapter, tkinter/ctypes Windows adapter, PowerShell, static HTML/CSS/JS, pytest, pytest-asyncio, Playwright, GitHub Actions.

## Global Constraints

- Python 支持范围固定为 `>=3.13,<3.14`；开发依赖必须包含 pytest、pytest-asyncio 和 Playwright。
- 服务器绝不读学员机 WorkBuddy DB、JSONL 或本地文件；`copilot.db` 是服务端唯一权威源。
- Hook 保持 stdlib-only、fire-and-forget、有界尾部读取，任何错误始终返回 0。
- Uvicorn 保持单 worker；不引入 Redis/MQ、死信队列、前端框架或大规模文件重构。
- 保留现有 REST/WS 字段，新字段使用向后兼容默认值；SQLite 只做幂等向前迁移。
- 消息“已送达”只能由学员端成功渲染并持久化后的 REST ack 产生，不得以 WebSocket 写入成功代替。
- 业务行为变更必须 TDD：先运行新测试见到预期 RED，再实现 GREEN；Service 集成使用真临时 Store + 固定 fake LLM，不 mock 被测 Service。
- 自动测试默认断网，使用独立临时 HOME/USERPROFILE/APPDATA/SQLite/spool/config；真 DeepSeek 只用于发布冒烟。
- Windows 是一等交付端，不再只做 contract 骨架：必须实现 WorkBuddy 读取、Hook/spool、常驻 runtime、导师消息、问答反馈、历史上传、浮标 UI、安装升级卸载、登录自启和 Windows CI。W0/W1 没有真机证据只阻断 rollout 声明，不阻断可由合成 fixture 和 `windows-latest` 证明的开发。
- Windows 浮标使用独立窄适配器，复用 Student Core 的状态与协议，不改写已经稳定的 macOS PyObjC NSPanel；Windows 合成 fixture 必须明确标为 synthetic，不得冒充真实 WorkBuddy 证据。
- 导师建议只能填入输入框，不自动发送，保留人的判断。
- AI 优先尝试解决学员问题；只有降级、未解决、连续低效或高置信风险才进入导师关注，不能把正常学习噪声推给导师。
- 诊断、问答和导师干预必须能追溯到有界但充分的会话上下文、历史摘要和证据；不得为降成本丢掉判断所需上下文。
- 容量验收使用真 Store/Service/EventBus/WSRegistry 与固定 fake LLM，覆盖 10/50/100/300 学员；不以引入 Redis、多 worker 或框架重写来掩盖单 worker 架构的真实边界。
- 成本优化必须在功能和容量基线冻结后执行，至少完成 25 个单变量实验；每个实验先记录假设、预期、指标和停止条件，再记录实际结果、失败路线与修正，不允许只写事后结论。
- 默认成本实验不得产生付费模型调用；若要运行真实付费 provider 对照，必须单独取得用户授权并保留调用预算。

---

### Task 1: Plan A — 可复现质量基线

**Files:**
- Create: `requirements-dev.txt`
- Create: `.python-version`
- Create: `.github/workflows/quality.yml`
- Create: `scripts/quality_summary.py`
- Modify: `README.md`, `docs/test-plan-v3.md`, `tests/conftest.py`, `copilot/wb_upload.py`, `tests/test_wb_upload.py`

**Interfaces:**
- `upload_conversations(..., data_adapter: WorkBuddyDataAdapter | None = None) -> dict[str, int]`; `None` 保留现有生产构建逻辑。
- `scripts/quality_summary.py --pytest-output <path> --output <path>` 从 pytest 结果生成机器可读摘要，README 只链接摘要，不手写 passed 数。

- [ ] 先在隔离 HOME 下运行 `tests/test_wb_upload.py` 中 6 个现有用例，记录 `not_installed` RED。
- [ ] 新增测试：传入合成 adapter 时只读 fixture，且 forbidden HOME 被访问会立即失败。
- [ ] 实现 adapter 注入，不增加仅测试可见的生产方法。
- [ ] 增加完整开发依赖、Python 版本声明和 Linux/macOS/Windows contract CI；macOS browser lane 安装 Chromium 后跑真 Playwright。
- [ ] 使测试收集阶段也使用临时配置和 DB，不创建 `~/.workbuddy-copilot`。
- [ ] 运行聚焦测试、`tests/test_platform_imports.py`、全量可用 lane 和 `git diff --check`；追加 RED/GREEN 到 `docs/dev-log.md`。
- [ ] 提交 `chore: make quality baseline reproducible`。

### Task 2: Plan B1 — Hook 事件幂等、Stop 持久恢复与有界重试

**Files:**
- Modify: `copilot/student_core/transport.py`, `copilot/student_core/coordinator.py`, `copilot/service.py`, `copilot/services.py`, `copilot/store.py`, `copilot/models.py`
- Test: `tests/test_student_transport.py`, `tests/test_student_coordinator.py`, `tests/test_analysis_service.py`, `tests/test_service_routing.py`, `tests/test_store.py`

**Interfaces:**
- `StudentTransport.post_hook(event: HookEvent, *, event_id: str = "") -> Accepted`; body 带可选 `event_id`。
- `ReportIn.event_id: str | None`; `POST /report` 返回 `duplicate: bool`。
- `AnalysisService.accept_report(...) -> AcceptedReport` dataclass，字段 `report_id, session_id, snapshot, duplicate, analysis_status`。
- `reports` 新增 `event_id`, `analysis_input`, `analysis_status`, `analysis_attempts`, `analysis_error`, `analysis_next_retry_at`；`(student_id,event_id)` 非空唯一。
- `AnalysisService.handle_stop_with_retry(..., max_attempts=3, sleeper=...)`；延迟为 0/1/5 秒，测试注入 fake sleeper。

- [ ] 先写并运行重复 `event_id` 的 HTTP 集成测试，证明旧实现产生重复 report/prompt/analysis。
- [ ] 先写并运行“202 后重启”测试，证明普通 Stop tail 无法恢复。
- [ ] 实现 event_id 端到端传递、幂等入库和重复请求无副作用返回。
- [ ] 在接收 Stop 时持久有界 `analysis_input`；成功后清除，失败保留以便恢复。
- [ ] 实现 3 次有界重试，每次原子更新尝试数和稳定错误码；启动时重放 pending/failed 但未超限的持久任务。
- [ ] 验证重复 10 次只有一条分析，且 provider 持续失败后状态/错误可查。
- [ ] 运行相关 P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: make hook analysis delivery durable`。

### Task 3: Plan B2 — 诊断可追溯、学员问答状态与原通道回归

**Files:**
- Modify: `copilot/llm.py`, `copilot/models.py`, `copilot/services.py`, `copilot/store.py`, `copilot/service.py`, `copilot/floating_native.py`
- Test: `tests/test_llm.py`, `tests/test_analysis_service.py`, `tests/test_student_ask_api.py`, `tests/test_floating_native_phase3.py`, `tests/test_e2e_reverse_message.py`, `tests/test_transcript_upload_api.py`

**Interfaces:**
- `AnalysisResult` 新增 `confidence: float = 0.5`, `evidence: list[str] = []`, `model: str = ""`, `prompt_hash: str = ""`, `latency_ms: int = 0`。
- `QuestionAnswerOutcome(status: Literal["answered","degraded","failed"], answer: str, error_code: str = "")`。
- `student_asks` 新增 `answer_status`, `error_code`, `feedback`, `feedback_note`, `feedback_at`。
- `POST /api/student/asks/{ask_id}/feedback` body `{student_id, feedback: "helpful"|"unresolved", note?: str}`。

- [ ] 先写诊断新字段的 JSON 解析/默认值/持久化 RED，包含 confidence 越界夹紧和 evidence 有界化（最多 3 条、每条 160 字）。
- [ ] 实现模型、prompt SHA-256、耗时和尝试信息入库，旧分析读取保持兼容。
- [ ] 先写问答 answered/degraded/failed 及 feedback 所有权 RED，再实现 outcome 和反馈接口。
- [ ] 学员浮标显示回答状态和“有帮助/未解决”；反馈不自动发导师消息。
- [ ] 增加导师消息 300 条、ack 响应丢失、重启、WS/REST 重复到达的系统回归；不改变送达语义。
- [ ] 回归上传同 SHA、仅重试诊断、部分失败和 stale result 丢弃。
- [ ] 运行相关 P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: make diagnosis and student asks traceable`。

### Task 4: Plan C — 诊断质量评测引擎与 60 条脱敏数据集

**Files:**
- Create: `copilot/evaluation.py`, `scripts/evaluate_diagnosis.py`
- Create: `tests/fixtures/evaluation/diagnosis_cases.jsonl`
- Create: `tests/test_evaluation.py`, `docs/diagnosis-evaluation.md`

**Interfaces:**
- JSONL case: `{id, category, transcript, latest_prompt, expected_attention, expected_reason_codes, acceptable_actions, forbidden_claims, reviewer_1, reviewer_2, adjudicated}`。
- `evaluate_cases(cases, analyzer) -> EvaluationReport`；报告字段 `total, json_valid_rate, high_precision, high_recall, normal_high_false_positive_rate, actionability_mean, gate_passed, failures`。
- CLI 输出 JSON 并在门槛未达到时非零退出。

- [ ] 先写指标计算 RED：99% JSON 有效率、90% high recall、80% high precision、正常样本 high 误报≤5%、可执行性均分≥4/5。
- [ ] 实现确定性评测计算、禁止声明命中检测和人工可执行性录入。
- [ ] 建立恰好 60 条合成脱敏样本：15 normal、15 technical_stuck、10 repeated_or_offtopic、10 insufficient_context、5 ask_failure、5 system_failure。
- [ ] `reviewer_1/reviewer_2/adjudicated` 缺任一时 CLI 必须报告 `human_review_incomplete` 并非零退出，不伪造双人审核完成。
- [ ] 增加 fake analyzer 的端到端评测测试；真 DeepSeek 只写入发布冒烟命令，不进默认 CI。
- [ ] 运行评测测试、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: add diagnosis quality evaluation gate`。

### Task 5: Plan D1 — Attention 领域、投影、幂等恢复与 API

**Files:**
- Create: `copilot/attention.py`, `tests/test_attention.py`, `tests/test_attention_api.py`
- Modify: `copilot/store.py`, `copilot/app_context.py`, `copilot/services.py`, `copilot/service.py`, `copilot/mentor/routes.py`, `copilot/connections.py`, `copilot/models.py`

**Interfaces:**
- `attention_items` 字段：`id, source_type, source_id, category, student_id, session_id, priority, reason_code, reason, evidence_json, suggested_action, confidence, status, handled_by, resolution_note, handled_at, created_at, updated_at`；`(source_type,source_id,reason_code)` 唯一。
- `AttentionService.project_analysis(analysis_id, result)`, `project_student_ask(ask_id)`, `project_system_failure(source_type, source_id, ...)`, `backfill_missing()`。
- `GET /api/mentor/attention?status=&priority=&category=&student_id=&limit=`。
- `PATCH /api/mentor/attention/{id}` body `{status, mentor_id, note}`，状态仅 `open|in_progress|resolved|dismissed`。

- [ ] 先写 AttentionPolicy 表驱动 RED，覆盖 error/stuck+confidence、最近 3 次 2 次 low/stuck、warn/off_topic、问答降级/未解决、系统失败和正常无项。
- [ ] 实现 Store 迁移、查询排序（high 先，同级 created_at 升序）和状态迁移；解决后新来源创建新项，不重开旧项。
- [ ] 投影先持久后发 `attention_updated`；WSRegistry 只向 mentors 推送，学员 float 不收到。
- [ ] 启动时扫描已有 analysis/student_ask 中缺失的投影，唯一约束保证重放无重复。
- [ ] 降级问答、`unresolved` 反馈和持续诊断/上传失败生成正确 category/priority 的关注项。
- [ ] `/api/mentor/students` 向后兼容增加 `open_attention_count`, `highest_attention_priority`, `last_attention_at`。
- [ ] 运行聚焦测试、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: add durable mentor attention projection`。

### Task 6: Plan D2 — 导师干预雷达交互

**Files:**
- Modify: `copilot/static/mentor/index.html`, `copilot/static/mentor/style.css`, `copilot/static/mentor/app.js`
- Test: `tests/test_mentor_frontend.py`, `tests/e2e/test_mentor_ui.py`

**Interfaces:**
- 前端 `state.attention = {items, filters, loading, error}`；所有用户可控文本经 `textContent`。
- 关注卡操作：`view`, `prefill`, `in_progress`, `resolved`, `dismissed`；`prefill` 只写 compose input，不 submit。

- [ ] 先写 Playwright RED：关注队列排序、过滤、查看对话、建议预填不自动发送、状态更新、WS 增量、loading/error/empty 和 XSS。
- [ ] 实现关注队列、两类标签（学习关注/系统异常）和 high/medium 显示，并保持原学员/对话/时间线/发消息功能。
- [ ] 点击卡片根据 student_id/session_id 调用现有选中流，不建第二套时间线状态。
- [ ] 导师学员列表按最高关注优先级、未处理数、最后活动排序，显示未处理数。
- [ ] 运行真浏览器聚焦测试、原 UI 全量 E2E、P0/P1 和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: add mentor intervention radar`。

### Task 7: Plan D3 — 导师台响应式和可用性门

**Files:**
- Modify: `copilot/static/mentor/index.html`, `copilot/static/mentor/style.css`, `copilot/static/mentor/app.js`
- Test: `tests/e2e/test_mentor_ui.py`

**Interfaces:**
- viewport `>=960px`: 关注区 + 现有三栏。
- viewport `600–959px`: 关注队列 + 导航/时间线双栏。
- viewport `<600px`: `attention|students|conversation` 三个单栏页签，消息输入区 sticky 且键盘可达。

- [ ] 先写 `1440x900`, `700x570`, `390x844` 的 Playwright RED，断言 `scrollWidth <= clientWidth`、可选学员/对话、时间线可见和 compose 可用。
- [ ] 实现断点、单栏页签和双栏导航，不复制现有 DOM 或 state。
- [ ] 增加键盘 focus、明确 button label、`aria-live` 状态和 `prefers-reduced-motion`。
- [ ] 生成三个 viewport 的验收截图到 Playwright 临时产物，不提交浏览器缓存。
- [ ] 运行真浏览器 UI 全量、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: make mentor radar responsive`。

### Task 8: Plan E1 — 学员身份强制派生与系统状态

**Files:**
- Modify: `copilot/app_context.py`, `copilot/service.py`, `copilot/config.py`, `config.example.json`, `copilot/student_core/transport.py`, `copilot/static/mentor/app.js`
- Test: `tests/test_public_auth.py`, `tests/test_app_context.py`, `tests/test_service_routing.py`, `tests/test_student_transport.py`, `tests/test_e2e_multistudent.py`, `tests/e2e/test_student_agent_system.py`

**Interfaces:**
- `StudentPrincipal(student_id: str, auth_mode: "mapped"|"shared")`；`require_student_principal()` 从 token 派生。
- `auth.allow_shared_student_token` 在 local/demo 默认 true，public/prod 默认 false；映射 token 存在时请求中 student_id 必须一致。
- `GET /api/mentor/system-status` 返回 `version, pending_analyses, failed_analyses, open_attention, float_connections, mentor_connections, windows_rollout_status`。

- [ ] 先建立 student route inventory 参数化 RED：学员 A token 不得通过显式或省略 student_id 读取/确认/同步/known SHA/upload status/ask/feedback/session/current/alerts/analysis catch-up/WS 等任何 B 数据；每条必须 401/403 且零副作用，省略 student_id 只能派生 A。
- [ ] 实现 principal 依赖并接入所有 student REST/WS；保留 local/demo 共享 token 兼容。
- [ ] 学员端始终传自己 student_id，服务端在 mapped 模式校验而不相信客户端作为权限源。
- [ ] 先写 system-status 真 Store/WSRegistry 集成 RED，再实现受 mentor token 保护的状态接口和导师台故障提示。
- [ ] `/health` 保持旧响应兼容；状态接口不返回 token、prompt、原文或 provider 响应。
- [ ] 运行鉴权/多学员/真 WS 回归、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: enforce student identity in pilot mode`。

### Task 9: Plan E2 — Windows runtime、WorkBuddy 数据与耐久链路

**Files:**
- Create: `copilot/student_core/process_liveness.py`, `copilot/student_core/transcript_jobs.py`, `copilot/student_platform/windows_runtime.py`
- Create: `tests/fixtures/workbuddy/windows_synthetic/`, `tests/test_windows_runtime.py`, `tests/test_windows_liveness.py`, `tests/test_windows_workbuddy_integration.py`, `tests/component/test_windows_student_runtime.py`
- Modify: `copilot/student_platform/workbuddy.py`, `copilot/student_platform/windows.py`, `copilot/student_core/spool.py`, `copilot/student_core/coordinator.py`, `copilot/student_core/transport.py`, `copilot/hook.py`, `copilot/wb_upload.py`, `copilot/models.py`, `copilot/store.py`, `copilot/service.py`, `start_student_agent.py`, `requirements-windows.txt`, `.github/workflows/quality.yml`

**Interfaces:**
- `ProcessIdentity(pid, started_at, owner_token)` 与 `ProcessLiveness.probe(identity) -> Literal["alive","dead","unknown","reused"]`；claim 持久化稳定 identity，只有 dead/reused 可自动回收，unknown fail closed 并进入健康状态。`--repair-claim <id> --expected-owner-token ... --reason ...` 只能在排他锁与匹配 identity 下写审计后修复，安装器不得静默清理。EventSpool 和 command claim 可注入，Windows 实现不调用 POSIX `os.kill(pid, 0)`。
- `WindowsWorkBuddyProfile` 只接受显式 W0 manifest/profile；生产环境缺真实 profile 时 transcript 能力 fail closed。`TranscriptScanner.read/index(...)` 保留现有 POSIX descriptor scanner，Windows scanner 显式处理 reparse point、Unicode/UNC/长路径、sharing violation 和目录逃逸。
- `TranscriptUploadQueue.enqueue(event_id, report_id, student_id, session_id)` 使用本地 SQLite/文件队列持久化 Stop 后全文补传；严格顺序为 `/report` 2xx → job durable commit → hook spool ack，全文 `/transcript` 2xx 后才删除 job，同 event_id/SHA 重放幂等。
- 自动 Stop 全文补传的权威语义固定为 `analysis_mode=store_only`：客户端携带 source event/report，服务端只在其能关联同 student/session 的 Stop report 且无 mentor request 时接受；全文只补上下文，不再触发 BulkUpload 分析/attention。Stop 尾部诊断仍是唯一即时分析，导师主动 upload request 才显式分析全文。
- `UploadOutcome(matched, attempted, accepted, skipped, failed, error_code)`；指定 session 必须恰好匹配且每个尝试都获服务端确认，不能把 `matched=0, failed=0` 当成功，只有 `complete=True` 才能写 command completion。
- `AnalysisEnvelope(type, student_id, session_id, report_id, event, result, timestamp)` 作为 WS 与 catch-up 的同一 wire shape；新增 `GET /api/student/analyses?after_report_id=&limit=` 按 report_id ASC 分页，返回 next cursor/has_more。StudentCoordinator 增加可选 `analysis_handler`，WindowsMessageStore 只在持久化/渲染成功后推进 cursor。
- analysis 恢复顺序固定为先建立 WS 并缓冲，再从本地 cursor 分页补拉至耗尽，再按 report_id 去重排空缓冲并进入实时；handler/store 失败不得推进 cursor。无 UI message_handler 的 headless runtime 不消费、不标 rendered、不 ack 导师消息，绝不伪造送达。
- `WindowsStudentRuntime` 统一构建 WindowsWorkBuddyData、uploader、StudentTransport、StudentCoordinator、StudentAgent、Stop transcript job drain 和有界 session/analysis sync；`start_student_agent.py` 只负责参数与平台选择。
- StudentTransport 保留同步兼容方法，新增 `post_hook_async/ack_message_async/get_pending_messages_async/get_recent_analyses_async`；Coordinator 必须优先调用 async 方法，旧同步 adapter 统一经 `asyncio.to_thread`，不得卡住唯一 WS 循环。

- [ ] 先写 Windows liveness RED，覆盖 alive/dead/unknown/PID reused、陈旧 claim、多 Agent 抢占、unknown health/审计修复，并证明 Windows 路径不触发 `os.kill`、安装器不自动删 ambiguous claim。
- [ ] 用明确标注 synthetic 的 Windows-shaped fixture 写 schema/JSONL/Unicode/mapping 解析 RED；sharing violation、reparse/junction 和长路径必须在 `windows-latest` 动态创建真实 Windows OS 对象验证，不能由静态 fixture 冒充。真实 UNC/SMB 与 WorkBuddy mapping 进入 W1；生产 mapping 只能来自 W0 manifest/profile，缺失时必须 typed blocked。
- [ ] 先写 Windows runtime RED：uploader 不得为 None；导师上传命令、指定 session、部分失败不落 completion、重启续传、session sync、analysis 实时/补拉和 WS 重连均走共享 Student Core。
- [ ] 写 Stop 全文耐久边界 RED：分别在 report 2xx 后、job commit 前后、spool ack 前后、全文发送响应丢失和进程重启时终止；最终 report/job/transcript 各一份，只有 Stop analysis/attention/模型调用各一份，自动全文补传不得产生第二份 BulkUpload 分析。
- [ ] 写 analysis catch-up RED：断线期间产生超过两页、WS 建连与分页并发产生新分析、重启、重复实时/补拉、handler/store 失败与跨学员参数；最终按 report_id 无漏无重，失败不推进 cursor。
- [ ] 写 headless handler RED：message_handler 缺失或失败时导师消息保持未 ack；接上真实耐久 inbox/UI handler 后才允许现有 REST ack。
- [ ] 修正 Hook 尾读失败语义：transcript 暂时被锁时仍写入空 tail 事件并在 2 秒内 exit 0，不静默丢 Stop。
- [ ] 将 Student Core 的同步 HTTP 移出 event loop；用阻塞 opener 证明 spool flush 不阻断 WS 收包、导师消息或 stop。
- [ ] 在 `windows-latest` 安装 core + windows + server + dev requirements，只收集 `windows and not real_machine`，跑 Windows adapter/install、Hook 真子进程、spool、transport、coordinator、agent、SQLite 和真 uvicorn loopback HTTP/WS，不再只跑 import contract；critical 用例不得 skip。W1 使用独立 runner/marker，缺证据输出 BLOCKED artifact 而非 hosted skip。
- [ ] 运行 Windows 聚焦、Linux/macOS 相邻回归、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: add durable Windows student runtime`。

### Task 10: Plan E3 — Windows 浮标、安装生命周期与一等 CI

**Files:**
- Create: `copilot/floating_windows.py`, `start_windows_client.py`, `uninstall_windows.ps1`, `run_windows_w1.ps1`, `scripts/validate_windows_evidence.py`
- Create: `tests/test_floating_windows.py`, `tests/test_windows_runtime_config.py`, `tests/test_windows_installer_lifecycle.py`, `tests/test_windows_evidence.py`, `tests/test_student_ask_idempotency.py`, `tests/component/test_windows_client_runtime.py`
- Create: `tests/fixtures/workbuddy/windows/evidence.schema.json`, `docs/windows-evidence-template.json`
- Modify: `copilot/student_core/transport.py`, `copilot/models.py`, `copilot/store.py`, `copilot/service.py`, `copilot/app_context.py`, `install_windows.ps1`, `register_hook.py`, `probe_windows_workbuddy.ps1`, `config.example.json`, `requirements-windows.txt`, `.github/workflows/quality.yml`, `README.md`, `docs/test-plan-v3.md`

**Interfaces:**
- `WindowsStudentView` 使用 tkinter/Windows API 的独立窄适配器：置顶可拖拽浮标、未读角标、消息/诊断面板、可靠当前会话或人工 session selector、提问、answered/degraded/failed、helpful/unresolved；不得 import 或修改 macOS PyObjC UI，无法可靠判断 active session 时不得把最近会话冒充当前。
- `WindowsMessageStore` 分别保留最多 300 条终态导师消息与 300 条终态诊断；pending/unrendered/ack-pending 永不因容量裁剪，终态按稳定时间/id 淘汰。mentor message 按 message_id、analysis 按 report_id 去重。UI 先幂等 upsert+render，成功返回后 Core ReceiptLedger 才记录 rendered 并调用现有单一 REST ack；两个 SQLite 写入之间崩溃时重放只补状态、不重复展示，不新增虚构的两阶段服务端回执。
- 学员提问增加可选向后兼容 `client_request_id`，服务端以 `(student_id, client_request_id)` 非空唯一；重复 POST 返回同一 pending/terminal ask 且不重复调用模型，并提供按 client_request_id 查询恢复。Windows 本地先持久 pending request，响应丢失后查询恢复再决定是否重试。
- `start_windows_client.py --config ... [--health-check]` 以 named mutex/稳定 lock 强制 single instance，Tk 在主线程，StudentAgent 在专用 non-daemon 线程和独立 asyncio loop；UI bridge 使用有界 Future/timeout，关闭/登出/关机时 bounded `agent.stop()` + join。UI pump 与 Agent loop 分别写 heartbeat 和滚动日志。
- 安装器固定 Python 3.13，使用显式 configDir/student/state/log 路径；token 只允许无回显交互输入或预置 `-TokenFile`，先校验来源 ACL，再写入已关闭权限继承且仅当前用户可读的 state 目录。原子 merge WorkBuddy settings，注册“仅用户登录且交互式”任务并配置失败重启；重复安装/升级幂等。卸载只按 owned-entry 标识移除 Copilot hook/task/venv；整份备份仅在 hash 未变化时恢复，绝不覆盖安装后新增的用户 hooks。
- `windows_rollout_status` 只能由 evidence validator 计算：artifact 必须通过 schema、hash 且匹配当前 commit/build；否则保持 `implementation_candidate` 或 blocked，普通配置布尔值不能宣称 rollout_ready。

- [ ] 先写纯 view-model/Tk adapter RED，覆盖浮标展开、未读、导师消息、实时/重启诊断、可靠/未知/手选 session、提问三态、反馈重试、键盘焦点、125%/150% DPI 和多屏边界。
- [ ] 写真 Student Core + 假 UI 线程桥接 RED：响应丢失、断网、重启、300+ pending/terminal 历史、WS/REST 重复到达和 MessageStore→ReceiptLedger 两写之间崩溃均只展示一次；非终态不得被裁剪，handler 失败不得 ack，渲染并持久化后才调用现有 REST ack。
- [ ] 增加 ask/feedback 的 StudentTransport 接口并接入 Windows UI；先写 client_request_id 重复 POST、响应丢失、进程重启和查询恢复 RED，确保只调用一次模型。token 派生身份必须沿用 Task 8，不允许 UI 覆盖其他 student_id。
- [ ] 先写 PowerShell 生命周期 RED：3.13 preflight、无回显/ACL 校验 TokenFile 输入、原子临时文件与 owned-entry merge、当前用户 ACL 保护 token/config/message DB/log/backup 且 token 不进命令行、Task action、日志或 PowerShell history，幂等安装/升级、交互式 Task Scheduler 自启/重启、single-instance、健康检查、hash-safe 回滚和卸载。
- [ ] 在 `windows-latest` 跑 headless UI presenter、runtime、PowerShell parser/contract 和真 loopback component test；任何 critical skip 失败。DPI/多屏 hosted 用例只证明计算/调用合同，实际截图、焦点、拖动、置顶、WorkBuddy/Git Bash Hook、杀软、中文用户目录、休眠唤醒和自启动必须由 W1 真机证明。
- [ ] 扩展 W0 probe、实现 evidence schema/validator 与 W1 runner，并把匹配当前 commit/build 的验证结果接回受鉴权 system-status；无真机证据时状态保持 `BLOCKED: real-machine evidence missing`，但不得再把 Windows 功能实现标成“后置”。
- [ ] 运行 Windows 聚焦、Linux/macOS 相邻回归、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: deliver first-class Windows student client`。

### Task 11: Plan E4 — 双平台发布门、试点运行手册与最终核验

**Files:**
- Create: `docs/pilot-runbook.md`, `docs/quality-baseline.md`
- Modify: `README.md`, `docs/test-plan-v3.md`, `docs/dev-log.md`

**Interfaces:**
- 试点规模固定 3–5 名学员、1–2 名导师、7 天。
- 发布记录必须含 commit SHA、Python/WorkBuddy/macOS/Windows 版本与 build、Windows CPU 架构、测试数、critical skip、诊断指标、evidence artifact hash、installer state manifest、真机结果和回滚命令。
- Windows 状态分为 `implementation_candidate`（hosted CI 通过）和 `rollout_ready`（W0/W1 真机通过）；缺 W0/W1 时可以完成代码候选，但 Task 11 不得标成双平台试点发布成功。

- [ ] 分别编写 Mac 与 Windows 的安装、Hook、休眠唤醒、断网恢复、原生浮标、删除试点数据和回滚可执行清单。
- [ ] 记录本地环境无 Python 3.13、人工双标注未完成、Windows W0/W1 或 7 天试点未运行等外部门为 blocked，不伪造通过。
- [ ] 在可用环境运行 Python 3.13 全量、Linux server、macOS client/browser、Windows client/system lane、真 Playwright 三 viewport、数据库迁移重开和 `git diff --check`。
- [ ] 核对总需求：event_id 10 次去重、202 后重启恢复、Stop 全文补传不丢、消息/诊断不重渲染、同 SHA 仅重试诊断、身份隔离、attention mentor-only、60 条评测、三 viewport，以及 Windows transcript 精确读取、导师消息一次展示、ask/feedback、安装升级卸载无设置丢失、重启/休眠、DPI/多屏、登录自启和完整身份隔离。
- [ ] 触发最终全分支 code review，修复所有 Critical/Important 发现并重验。
- [ ] 提交 `docs: add pilot release gates and runbook`。

### Task 12: Plan F — 百人级容量、故障与浸泡验证

**Files:**
- Create: `copilot/scale_validation.py`, `scripts/run_scale_validation.py`
- Create: `tests/test_scale_validation.py`, `tests/test_scale_validation_cli.py`
- Create: `docs/scale-validation.md`
- Modify: `docs/test-plan-v3.md`, `docs/dev-log.md`

**Interfaces:**
- `ScaleScenario(students, reports_per_student, ws_fraction, duplicate_rate, failure_mode, restart_at)`。
- `run_scale_scenario(scenario, harness) -> ScaleReport`；报告至少包含 `accepted, completed, lost, duplicates, failed, pending, attention_created, p50_ms, p95_ms, p99_ms, max_queue_depth, sqlite_busy_errors, peak_rss_mb, db_bytes`。
- CLI 支持 `--students 10,50,100,300 --output <json>`，每个场景写独立原始指标和总 gate；任何数据丢失、跨学员串线、重复副作用或未发现积压都非零退出。

- [ ] 先写确定性 RED：10/50/100/300 学员并发上报、重复 event_id、部分 WS 断线、LLM 超时/失败、202 后重启和恢复 drain。
- [ ] harness 使用真临时 SQLite、Store、Service、EventBus、WSRegistry 与固定 fake LLM；不得 mock 被测 Service，也不得访问外网或真实用户目录。
- [ ] 覆盖导师消息/attention 定向 fanout，证明慢连接或断开学员不会阻塞其他学员，且 mentor-only 事件不进入学员浮标。
- [ ] 覆盖完整上下文与历史摘要选择：高并发下不串 student/session，不因队列合并丢证据，不把系统异常误判为学习异常。
- [ ] 固定容量门：lost=0、重复副作用=0、跨学员泄漏=0、SQLite busy 未静默、attention 可见 p95≤30 秒、重启后 60 秒内 drain；超时必须保留 pending/failed 证据。
- [ ] 运行 10→50→100→300 阶梯压力；至少一次 30 分钟等价浸泡（可用加速事件时钟，但必须另跑真实墙钟短 soak）并记录 RSS、DB 增长、队列水位与延迟分位数。
- [ ] 在 `docs/scale-validation.md` 写清可承载证据、瓶颈、停止条件和仍需真实试点验证的边界；不得把 fake LLM 结果冒充真实 provider 容量。
- [ ] 运行聚焦、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `test: add multi-student scale and failure gates`。

### Task 13: Plan G — 成本优化实验控制器与不少于 25 组实测

**Files:**
- Create: `copilot/cost_experiments.py`, `scripts/run_cost_experiments.py`
- Create: `tests/test_cost_experiments.py`, `tests/test_cost_experiments_cli.py`
- Create: `tests/fixtures/cost/baseline_cases.jsonl`
- Create: `docs/cost-experiments/index.md`, `docs/cost-experiments/results.jsonl`
- Modify: `docs/diagnosis-evaluation.md`, `docs/dev-log.md`
- Mirror final records to: `/Users/xiaoshushenxia/Documents/测试/🏭项目/超脑ai夏令营/黑客松/`

**Interfaces:**
- `ExperimentSpec` 固定字段：`id, hypothesis, expected_result, baseline_id, single_variable, dataset, metrics, stop_condition`。
- `ExperimentResult` 固定字段：`actual_result, verdict, failed_routes, corrections, quality_delta, estimated_token_delta, estimated_cost_delta, latency_delta, artifacts, started_at, finished_at`。
- `run_experiment(spec, runner) -> ExperimentResult`；开始运行前先持久化 spec，结束或失败后原地追加 actual/failure/correction，不得覆盖原假设。
- CLI `--catalog <jsonl> --results <jsonl> --resume`；少于 25 个完成实验、缺少任一必填记录或质量门下降时非零退出。

- [ ] 先写日志完整性与 resume RED：中断后保留已写 hypothesis/expected，重启不重复实验，不允许事后补写伪装成事前假设。
- [ ] 冻结同一 baseline commit、同一 60 条诊断集、同一规模场景、同一计价快照；每个实验只改变一个变量并记录停止条件。
- [ ] 实际完成不少于 25 组实验，至少覆盖：上下文窗口/选择、历史摘要复用、正常学习短路、prompt 压缩、evidence 上限、model routing、max_tokens、重试/退避、并发、缓存、SHA/事件去重、问答与 attention 合并。
- [ ] 每组记录假设→预期→实际→失败路线/修正；失败、无收益和回退实验同样保留，不能只留下成功样本。
- [ ] 质量保护沿用 Plan C 门；任何高优先级召回、正常误报、证据真实性或建议可执行性越界的省钱方案自动判为 rejected。
- [ ] 默认运行离线 deterministic/fake provider 以测调用数、token 估算、缓存命中、延迟与质量；真实 provider 对照必须在获得用户付费授权后另列，不混写为已完成。
- [ ] 输出推荐组合时只叠加已证明相容的变量，并用一次组合复验确认总收益不是单项估算相加；记录被否决方案和原因。
- [ ] 将完整 Markdown/JSONL 结果复制到用户指定黑客松目录；只新增或更新本项目实验记录，不删除该目录任何现有文件。
- [ ] 运行实验控制器测试、25+ 实验重放、质量/容量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `perf: add reproducible cost optimization experiments`。
