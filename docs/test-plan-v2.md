# WorkBuddy Copilot — 测试方案 v2（目标架构 / 闭环用）

> 生成日期：2026-07-02 | 对应 [target-architecture.md](target-architecture.md) + [reuse-map.md](reuse-map.md)
> 修订日期：2026-07-06 | 公网 MVP 补充：token 鉴权、离线同步补拉、owner 防线、同步失败提示
> 二次修订：2026-07-06 | 学员端技术助教体验：可更新过程提醒提示词、简化面板、当前会话自动跟随
> 用途：供**自动闭环工作流**确定性判定（每项含明确命令 + 通过判据）+ codex 开发的验收基线。
> 运行环境：项目内 venv（`venv/bin/python`），仓库根目录。
> 约定：`$PY = venv/bin/python`；所有命令先 `cd` 仓库根。
> **⚠ 锁定门：本方案（含后端/前端/场景/视觉全部）必须在 codex 开发（尤其 worktree 并行）之前经用户确认锁定。锁定后判定标准不允许修改（命令可按环境调整并记 dev-log）。**
> 覆盖面：后端(§P0/P1) · 公网鉴权与同步容错 · 反向通道 · **前端浏览器(§P1-FE)** · **端到端场景(§P2)** · **视觉保真(§视觉)** · 保真矩阵与人类残留(§保真)。

## 2026-07-06 公网 MVP 补充原则

- 后续改代码前，必须先为本节新增项补测试，并先见红。
- 生产/公网语义：配置 token 后，导师台 REST/WS 必须可用；错误 token 必须被拒；空 token 只允许本地开发。
- 学员 token 与导师 token 分离；学员 token 不能读导师 API。
- 导师触发上传必须有 `upload_requests` 状态闭环：pending/running/done/failed；浮标离线后重连能补拉 pending request。
- 历史 LLM 诊断不要求严格恢复，但失败必须可见、内容入库不回滚、用户可重试。
- P0-6 红线需要从“简单 grep 字符串”升级为“服务端模块不读本地 WorkBuddy FS/DB；学生端模块显式 allowlist”。不允许通过字符串拼接绕过红线。
- 学员端浮标不是多 tab 工具；顶部是会话切换，主体是建议/导师提示，底部是助教问答。
- 过程提醒提示词必须可更新；当前由配置/管理员更新，未来导师端可扩展为全员提示词编辑。
- WorkBuddy 当前会话自动跟随只要求学员端浮标使用，不要求导师台展示。
- 当前共享 `student_token` 仅证明学员端角色；客户端提供的 `student_id` 尚未绑定认证 principal。按 `student_id` 查询不是授权隔离，当前部署不得称为学员级或租户级隔离。
- 本轮只测试未接线的 `student_id_for_token()` 迁移接缝：映射唯一匹配时解析，缺失/异常/空/未知/重复 token 时 fail closed；共享 token 和现有 HTTP/WS 授权行为必须保持不变。只有未来路由从 principal 派生 `student_id` 并拒绝不匹配值后，才可缓解持共享 token 冒充他人并读取/确认消息或连接 `/ws` 的风险。

---

## P0 生存测试（每次迭代必过，退出码=0）

| # | 项 | 命令 | 判据 |
|---|----|------|------|
| P0-1 | 依赖可导入 | `$PY -c "import copilot.service"` | 退出码 0 |
| P0-2 | FastAPI app 可构建 | `$PY -c "from copilot.service import app; print(type(app))"` | 打印 FastAPI，退出码 0 |
| P0-3 | 组合根可构建依赖 | `$PY -c "from copilot.app_context import build_context; c=build_context(); print(bool(c))"` | True，退出码 0 |
| P0-4 | 全量测试通过 | `$PY -m pytest tests/ -q` | 退出码 0，0 failed |
| P0-5 | 单 worker 断言生效 | `COPILOT_WORKERS=2 $PY -c "from copilot.app_context import assert_single_worker; assert_single_worker()"` | 非法 worker 数拒绝启动（非 0 退出/抛错）|
| P0-6 | 无残留单机红线 | `$PY -m pytest tests/test_server_redlines.py -q` | 服务端模块不读取学员机 `workbuddy.db`/JSONL/本地 FS；学生端读取模块必须在 allowlist，且测试能抓到字符串拼接绕过 |
| P0-7 | 公网 token 不可空启动检查 | `$PY -m pytest tests/test_public_auth.py -q` | public/prod 模式下未配置 student_token/mentor_token 拒绝启动；local/dev 模式仍可空 token |
| P0-8 | 部署脚本公网参数检查 | `$PY -m pytest tests/test_deploy_config.py tests/test_config.py::TestUrlBuilders -q` | config/example 和启动脚本支持公网 base_url/host override、student_token、mentor_token；客户端 `public_base_url=https://...` 自动转 HTTPS/WSS |

## P1 核心功能（单元 + 集成，行为测试非 mock 空转）

| # | 领域 | 命令 | 判据 |
|---|------|------|------|
| P1-1 | transcript 解析（字节入参）| `$PY -m pytest tests/test_transcript.py -q` | parse_text(bytes) 正确提取 message/tool/ai-title/session_id；坏行跳过 |
| P1-2 | Store CRUD + 新表 | `$PY -m pytest tests/test_store.py tests/test_store_mentor.py -q` | 5 新表建成；prompts 全文不截断；FK ON DELETE CASCADE 生效；raw_transcripts 整篇落盘 |
| P1-3 | **severity 两语义（防吞 error）** | `$PY -m pytest tests/test_store.py -k severity -q` | 会话含 error+info 时：圆点=error（最坏严重度）；last_diagnosis=最新那条。**断言：有 error 的会话圆点绝不显示绿灯** |
| P1-4 | **timeline UNION 补列** | `$PY -m pytest tests/test_store_mentor.py -k timeline -q` | analysis 条目带 severity/suggestion/is_technical/topic（非空/非默认）；三类型按 created_at 排序 |
| P1-5 | **AnalysisService 集成（真 Store+假 LLM）** | `$PY -m pytest tests/test_analysis_service.py -q` | handle_stop 真实执行：三表落库、ai_summary.prompt_id 外键正确、seq 正确、发 prompt/ai_summary/analysis 三事件、raw_transcripts 落全文 |
| P1-6 | LLM 封装 + 降级 | `$PY -m pytest tests/test_llm.py tests/test_llm_summary.py -q` | JSON 解析/code fence 剥离/缺字段填充/非法降级；consent 软门控不硬拦 |
| P1-7 | /report 分流 + 202 | `$PY -m pytest tests/test_service_routing.py -q` | UserPromptSubmit 存 prompt 不调 LLM；Stop 返回 **202** 且 BackgroundTask 触发 LLM；不直连 STORE/parse_transcript |
| P1-8 | 导师 API + DI | `$PY -m pytest tests/test_mentor_api.py -q` | students(带 display_name)/sessions/timeline 正常；用 dependency_overrides 注入；无 `from .. import service` 反向依赖 |
| P1-9 | hook 上报（尾部字节+降级）| `$PY -m pytest tests/test_hook.py -q` | payload 含 transcript 尾部字节（非本地路径）+ 每机 student_id + token；解析异常降级只送元数据仍 return 0 |
| P1-10 | 前端结构 | `$PY -m pytest tests/test_mentor_frontend.py -q` | app.js 含内存 state（不从 DOM 反读）；用 textContent（无 innerHTML 拼学员内容）；重连带 last_seen_message_id；有发消息 POST |

## P1 反向通道 + WS（D2 核心新功能）

| # | 项 | 命令 | 判据 |
|---|----|------|------|
| P1-11 | WSRegistry 寻址 + 隔离 | `$PY -m pytest tests/test_connections.py -q` | floats=dict[sid,set]/mentors=set；正向事件广播导师池+定向本人浮标；反向 mentor_message 定向目标浮标+回显导师；两池物理隔离 |
| P1-12 | WS 扇出超时剔除 | `$PY -m pytest tests/test_connections.py -k timeout -q` | 塞一个 send 阻塞/抛异常的死连接：gather+wait_for 超时剔除它，其余客户端仍收到 |
| P1-13 | 反向消息幂等 + 补拉 | `$PY -m pytest tests/test_message_service.py -q` | 消息先落库(delivered_at=NULL)再 publish；在线送达置 delivered_at；离线 GET /api/student/messages?since 补拉 id>last_seen；message_id 客户端幂等去重（重复不重渲染）|
| P1-14 | 级联删除接缝 | `$PY -m pytest tests/test_store.py -k cascade -q` | DELETE 学员单事务级联删 6 表、返回删除行数；子表先行不违 FK |

## P1 公网鉴权 + 同步容错补强（2026-07-06 新增）

| # | 项 | 命令 | 判据 |
|---|----|------|------|
| P1-15 | 双角色 token 鉴权 | `$PY -m pytest tests/test_public_auth.py -q` | mentor token 可访问导师 REST/WS；student token 可访问 /report、学员 WS、学员上传/补拉；student token 访问导师 API 返回 401/403 |
| P1-16 | 导师台前端携带 token | `$PY -m pytest tests/test_mentor_frontend.py -k token -q` | 真实 app.js 的 fetch 带 Authorization/X-Copilot-Token，/ws/mentor 带 mentor token；401 时提示重新认证 |
| P1-17 | upload_requests 离线补拉与状态闭环 | `$PY -m pytest tests/test_upload_requests.py -q` | 浮标离线时导师 request-upload 不丢；浮标重连 GET pending 后执行；状态 pending→running→done/failed；失败记录 error_message |
| P1-18 | session owner 防线 | `$PY -m pytest tests/test_store.py -k owner -q` | 既有 session_id 属于 A 时，B 的 upsert/report/upload 不能更新 A 的 title/group/activity，也不能把 timeline 混入 A |
| P1-19 | bulk upload 诊断失败可见 | `$PY -m pytest tests/test_transcript_upload_api.py -k failure -q` | 上传内容已入库；后台 LLM 失败时标记诊断 failed/待重试；接口返回或导师台可展示失败状态；同 sha 不因上次诊断失败而永久跳过重试 |
| P1-20 | 旧 mentor WS 模块不再可用 | `$PY -m pytest tests/test_server_redlines.py -k mentor_ws -q` | `copilot/mentor/ws.py` 不存在于 runtime tree；无 token 的旧 WS 池不能被误用 |
| P1-21 | 过程提醒提示词可更新 | `$PY -m pytest tests/test_prompt_config.py tests/test_llm.py::TestBuildSystemPrompt -q` | `process_reminder_prompt` 来自配置/Store，不硬编码；更新提示词后新分析使用新版本；system prompt 明确“少而准/不要频繁打断” |
| P1-22 | 学员助教问答上下文优先级 | `$PY -m pytest tests/test_student_ask_api.py -q` | 有当前 session 时优先用该会话 raw/messages；无当前会话时回退最近 analyses；LLM 失败返回降级回答 |
| P1-23 | 学员端当前会话跟随 | `$PY -m pytest tests/test_floating_native_phase3.py -k current_session -q` | 面板关闭时检测到 WorkBuddy 当前会话变化会自动切换；面板打开时不强制切走，只更新当前会话标记 |
| P1-24 | 学员身份迁移接缝（未接线） | `$PY -m pytest tests/test_app_context.py tests/test_public_auth.py tests/test_deploy_config.py -q` | `student_tokens` 恰好一个非空字符串 token 匹配才返回学员；缺失/格式异常/空/未知/重复歧义返回 None；共享 token 与既有 HTTP/WS 授权不变；示例配置含空映射 |

## P1-FE 前端（浏览器 / Playwright，[前端]/[端到端]）

> 工具：Playwright（已装）。需真实拉起服务：用独立测试端口（如 18765，避开旧服务 8765）+ 临时 copilot.db；或用 Playwright 连 TestClient/ASGI。每项须先"见过红"（负控）。

| # | 标注 | 步骤 | 判据 |
|---|------|------|------|
| FE-1 | [前端] | 打开 /mentor/ | 三栏渲染；学员列表来自 GET /api/mentor/students（非空、带 display_name）|
| FE-2 | [前端] | 点学员→点对话 | 中栏对话列表、右栏时间线渲染，三类型着色（蓝/紫/橙）。**回归断言 B3：点任一对话后其他学员/会话的状态圆点不被刷绿、告警/分析计数不清零** |
| FE-3 | [端到端] | 学员侧 /report(UserPromptSubmit+Stop) → 导师台 | 导师台 WS 实时新增 prompt(蓝)/ai_summary(紫)/analysis(橙) 条目，无需刷新 |
| FE-4 | [端到端] | 导师在 compose 框发消息→发送 | POST /api/mentor/message 成功；假浮标 WS 收到 mentor_message 且**仅目标学员收到**；学员端成功渲染并持久化回执后，导师台显示"已展示" |
| FE-5 | [前端/安全] | 学员把会话标题设为 `<img src=x onerror=alert(1)>` | 导师台把它**当纯文本渲染、不执行 JS**（textContent，XSS 回归）|
| FE-6 | [前端] | WS 断开后重连 | 重连带 last_seen_message_id 触发补拉，时间线无缺口、无重复（message_id 幂等）|
| FE-7 | [前端/安全] | 配置 mentor token 后打开 /mentor/ | 首次要求输入或读取 mentor token；所有 REST/WS 请求带 token；token 错误时显示认证失败而不是静默空白 |
| FE-8 | [前端/端到端] | 导师触发同步时浮标离线→重连 | 导师台先显示 pending；浮标重连后补拉并执行；成功显示 done，失败显示 failed + 可重试 |
| FE-9 | [学员端/结构] | 打开浮标面板 | 顶部为“当前/最近对话切换”而非功能 tab；主体展示当前建议/导师提示；底部展示助教问答输入 |

## 视觉保真验证（[视觉]，per closed-loop 3.4c）

> 导师台复用现有 style.css（REUSE_ASIS），视觉基本沿袭旧版；仍须纳入闭环防跑偏。

**前置门（写 UI 前）**：设计基线 = 现有 style.css 配色令牌 + 本节的纯文字视觉约束（历史实机截图已因隐私要求移除）。锁定关键令牌：状态点 red/yellow/green、时间线 prompt=蓝 / ai_summary=紫 / analysis=橙、三栏 grid 布局。

| # | 标注 | 判据 | 负控（防假绿）|
|---|------|------|------|
| V-1 | [视觉] | /mentor/ 截图：三栏 grid 存在；固定取色点 hex 命中三类型着色 + 状态点三色（容差在开跑前与用户锁定）| 故意把 analysis 橙令牌改错一档 → V-1 必须变红。从没红过的视觉用例无效 |
| V-2 | [视觉] | 导师提示气泡样式（mentor_message 新增气泡）在时间线可辨识 | 移除气泡样式类 → 断言失败 |

**浮标（PyObjC）视觉 = 人类残留**：跨 Space 显示、点击弹卡片、导师提示气泡、红点脉冲 → 真机人工冒烟，不纳入自动化。

## P2 端到端场景

### 场景 S1：多学员数据隔离（防串号，D1/D4）
```
$PY -m pytest tests/test_e2e_multistudent.py -q
```
学员 A、B 各走 /report(UserPromptSubmit+Stop)。**判据**：A 的 timeline/sessions/students 计数不含 B 的数据；导师 API 分别查 A/B 互不串；session 归属正确（若 session_id 非全局 UUID 则复合键隔离）。

### 场景 S2：导师→学员浮标反向消息（D2）
```
$PY -m pytest tests/test_e2e_reverse_message.py -q
```
假浮标(student=S)连 WSRegistry → 导师 POST /api/mentor/message{student_id:S} → **判据**：该浮标收到 mentor_message、其他学员浮标不收到、导师池收到回显；浮标离线时消息落库、重连带 last_seen 补拉且不重复。

### 场景 S3：跨机 transcript 内容上行（D1/D3）
```
$PY -m pytest tests/test_transcript_upload_api.py -q
```
模拟 hook 上传 transcript 尾部字节（服务端不读 FS）→ **判据**：服务端 parse_text 解析成功喂 LLM；raw_transcripts 落**完整原文**（非尾部截断）；服务端全程不触碰任何本地 transcript 路径。

### 场景 S4：公网鉴权与角色隔离（D7）
```
$PY -m pytest tests/test_public_auth.py tests/e2e/test_mentor_ui.py -k auth -q
```
配置 mentor/student 两类 token。**判据**：导师台能在配置 token 后正常加载；学员 token 不能访问导师列表/时间线；导师 token 不用于学员 hook；错误 token 均被拒并给 UI 明确提示。

### 场景 S5：导师触发全量同步的容错（D8）
```
$PY -m pytest tests/test_upload_requests.py tests/test_transcript_upload_api.py -k "request or failure" -q
```
导师触发同步时浮标离线。**判据**：request 入库 pending；浮标重连补拉后变 running；上传成功变 done；客户端/LLM 失败变 failed 且展示错误；重试不会重复写脏数据。

### 场景 S6：学员端技术助教体验（D10-D12）
```
$PY -m pytest tests/test_student_ask_api.py tests/test_prompt_config.py tests/test_floating_native_phase3.py -k "ask or reminder or current_session" -q
```
模拟学员在 WorkBuddy 切换当前会话、向 Copilot 提问、触发过程提醒。**判据**：浮标默认跟随当前会话；提问回答使用当前会话上下文；过程提醒由可更新提示词控制；导师台不需要出现当前会话状态。

## P3 手动/半自动验收（闭环外，rollout 前）

- [ ] 公网 HTTPS/WSS 入口：导师浏览器与学员机均通过公网域名访问，不依赖 LAN。
- [ ] 单 worker 部署：`uvicorn copilot.service:app --workers 1` 或单 worker 进程，多 worker 启动被拒。
- [ ] 配置 mentor/student token 后：导师台可加载，学员机可上报，错误 token 被拒。
- [ ] 灰度 2-3 台学员机 hook 远程上报 → 中心 copilot.db 收到，导师台可见。
- [ ] session_id 全局唯一性核实（决定是否复合主键）。⚠ 阻塞项，Phase 1 前完成。
- [ ] 浮标收到导师提示气泡（真机 NSPanel 跨 Space）。

---

## 保真矩阵与人类残留（防假覆盖）

| 能力 | 被真实验证 | mock/stub（标"未验证"）| 人类残留（真机/人工冒烟）|
|------|-----------|----------------------|------------------------|
| transcript 解析 | P1-1（真字节输入 + 断言内容）| — | — |
| LLM 分析 | P1-5/P1-6（假 LLM 返固定 dict 驱动真链路）| 真实 DeepSeek 网络调用在单测层不打，靠假 LLM | rollout 前用真 Key 冒烟一次 |
| 落库/隔离 | P1-2/P1-5/S1（真临时 Store）| — | — |
| 反向通道 | P1-11~14/FE-4/S2（真 WSRegistry + 假浮标）| — | 浮标真机收气泡 |
| 导师台 UI | FE-1~6 + V-1/V-2 | — | — |
| 浮标 UI（PyObjC）| — | — | 跨 Space/点击/气泡/脉冲：真机人工 |
| 跨机 rollout | S3（模拟上行）| — | 灰度 2-3 台真机 hook 远程上报 |
| 公网鉴权 | P1-15/P1-16/S4 | — | 真实域名 HTTPS/WSS 冒烟 |
| 上传容错 | P1-17/P1-19/S5 | — | 浮标真机离线→上线补拉冒烟 |
| 学员端技术助教 | P1-21/P1-22/P1-23/S6 | LLM 真实调用用假 LLM；真机 UI 仍需人工 | 真机切换 WorkBuddy 会话 + 提问 + 过程提醒冒烟 |

> 铁律：正在验证的链路禁止用 mock 顶替（P1-5 必须真 Store+假 LLM，不许 patch 掉 handle_stop）；fixture 用确定性内容并断言输出内容，不止"非空/没报错"。

## 追溯矩阵（需求 → 测试）

| 需求/决策 | 测试 |
|-----------|------|
| D1 多机 + 跨机 transcript | P0-6, P1-9, S3 |
| D2 反向通道（导师→浮标）| P1-11~13, S2 |
| D3 完整对话原文落库 | P1-2, P1-5, S3（raw_transcripts 全文断言）|
| D4 导师看全部 + 数据隔离 | P1-8, S1 |
| 真 bug B1 severity 吞 error | P1-3 |
| 真 bug B2 timeline 丢列 | P1-4 |
| 真 bug B3 前端伪状态/XSS | P1-10 |
| 真 bug B8 handle_stop 零测试 | P1-5 |
| 真 bug B9 202+BackgroundTask | P1-7 |
| 单 worker 铁律 | P0-5, P3 |
| D7 公网服务器连接 | P0-7, P0-8, P1-15, P1-16, S4, P3 |
| D8 同步容错与提示 | P1-17, P1-19, FE-8, S5 |
| D10 学员端技术助教浮标 | P1-22, P1-23, FE-9, S6 |
| D11 过程提醒提示词可更新 | P1-21, S6 |
| D12 当前会话跟随仅学员端 | P1-23, S6 |
| MVP 共享 token 身份边界与迁移接缝 | P1-15, P1-24, S4 |
| 分层无泄漏 | P0-6, P1-7, P1-8 |
| 导师台前端行为（状态源/XSS/实时/补拉）| FE-1~6, P1-10 |
| 视觉保真（配色/布局/气泡）| V-1, V-2（浮标真机=人类残留）|
| 反向消息仅目标学员收到 | FE-4, S2 |

## 测试策略红线（防虚假绿灯）

- **禁止** patch 掉被测对象本身（如 patch analysis_svc 再断言"调了它"）。Service 集成测试必须用**真 Store（临时 db）+ 假 LLM（返回固定 dict）** 真实驱动 handle_stop。
- **禁止**脆弱断言：不硬编码 LLM 截断阈值数字、不靠 time.sleep 排序判 latest（用显式 created_at 注入）。
- 前端测试须断言**行为特征**（内存 state/textContent/补拉），不止文件存在 + 关键字包含。
- 每个外部依赖（LLM/DB/WS）都要有"挂了不崩 + 能查日志"的降级测试。
