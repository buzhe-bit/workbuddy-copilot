# WorkBuddy Copilot 原闭环加固与导师干预雷达 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变现有 Controller–Service–Repository、EventBus、Student Core、本地 spool、SQLite、单 worker 和静态导师台架构的前提下，建立可复现质量门、加固原闭环，并交付 AI 诊断评测、导师干预雷达与小范围试点保障。

**Architecture:** 服务端继续以 FastAPI 路由调用 Service，Service 通过 Store 持久化并经 EventBus/WSRegistry 推送。Hook 仍 stdlib-only，Student Core 仍负责 spool/HTTP/WS/回执。新增 attention 是已持久化 analysis/student_ask/system 结果的可重建投影，不引入 broker、Redis、多 worker 或前端框架。

**Tech Stack:** Python 3.13, FastAPI, SQLite, asyncio/websockets, stdlib Hook, PyObjC macOS adapter, static HTML/CSS/JS, pytest, pytest-asyncio, Playwright, GitHub Actions.

## Global Constraints

- Python 支持范围固定为 `>=3.13,<3.14`；开发依赖必须包含 pytest、pytest-asyncio 和 Playwright。
- 服务器绝不读学员机 WorkBuddy DB、JSONL 或本地文件；`copilot.db` 是服务端唯一权威源。
- Hook 保持 stdlib-only、fire-and-forget、有界尾部读取，任何错误始终返回 0。
- Uvicorn 保持单 worker；不引入 Redis/MQ、死信队列、前端框架或大规模文件重构。
- 保留现有 REST/WS 字段，新字段使用向后兼容默认值；SQLite 只做幂等向前迁移。
- 消息“已送达”只能由学员端成功渲染并持久化后的 REST ack 产生，不得以 WebSocket 写入成功代替。
- 业务行为变更必须 TDD：先运行新测试见到预期 RED，再实现 GREEN；Service 集成使用真临时 Store + 固定 fake LLM，不 mock 被测 Service。
- 自动测试默认断网，使用独立临时 HOME/USERPROFILE/APPDATA/SQLite/spool/config；真 DeepSeek 只用于发布冒烟。
- Windows W0/W1 没有真机证据时必须保持 `BLOCKED: real-machine evidence missing`，不得宣称可 rollout。
- 导师建议只能填入输入框，不自动发送，保留人的判断。

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

- [ ] 先写学员 A token 访问/确认/上传/连接学员 B 的 HTTP/WS RED，每条必须 401/403 且不改数据。
- [ ] 实现 principal 依赖并接入所有 student REST/WS；保留 local/demo 共享 token 兼容。
- [ ] 学员端始终传自己 student_id，服务端在 mapped 模式校验而不相信客户端作为权限源。
- [ ] 先写 system-status 真 Store/WSRegistry 集成 RED，再实现受 mentor token 保护的状态接口和导师台故障提示。
- [ ] `/health` 保持旧响应兼容；状态接口不返回 token、prompt、原文或 provider 响应。
- [ ] 运行鉴权/多学员/真 WS 回归、P0/P1、全量回归和 `git diff --check`；追加 dev-log。
- [ ] 提交 `feat: enforce student identity in pilot mode`。

### Task 9: Plan E2 — 发布门、试点运行手册与最终核验

**Files:**
- Create: `docs/pilot-runbook.md`, `docs/quality-baseline.md`
- Modify: `README.md`, `docs/test-plan-v3.md`, `docs/dev-log.md`

**Interfaces:**
- 试点规模固定 3–5 名学员、1–2 名导师、7 天。
- 发布记录必须含 commit SHA、Python/WorkBuddy/macOS 版本、测试数、critical skip、诊断指标、真机结果和回滚命令。

- [ ] 编写 Mac 安装、Hook、休眠唤醒、断网恢复、原生浮标、删除试点数据和回滚的可执行清单。
- [ ] 记录本地环境无 Python 3.13、人工双标注未完成、7 天试点未运行等外部门为 blocked，不伪造通过。
- [ ] 在可用环境运行 Python 3.13 全量、Linux/macOS/Windows contract lane、真 Playwright 三 viewport、数据库迁移重开和 `git diff --check`。
- [ ] 核对总需求：event_id 10 次去重、202 后重启恢复、消息不重渲染、同 SHA 仅重试诊断、身份隔离、attention mentor-only、60 条评测和三 viewport。
- [ ] 触发最终全分支 code review，修复所有 Critical/Important 发现并重验。
- [ ] 提交 `docs: add pilot release gates and runbook`。
