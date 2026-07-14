# Task 3 独立代码审查

## 结论

**不通过（NOT APPROVED）**

审查范围：`95bed8ff7ed551f9ed8f5677782cd8cd123b65cd..dd8daad726fb85de3dd750c52641a7b3786cd92b`。

- Critical：0
- Important：3
- Minor：0

虽然指定回归和相邻 Task 2 回归均通过，但当前绿测漏掉或反向固化了三个核心语义：
bulk 同 SHA 排他领取、provider 实际模型来源、未知 session 的 ask-first 原子绑定。因此不满足
“只有 Critical=0 且 Important=0 才可 APPROVED”的审查门。

## Critical

0。

## Important

### I1. Bulk 同 SHA 的“原子领取”不是 CAS，并发执行会双调 LLM、双写分析

- 位置：`copilot/service.py:371-379`、`copilot/service.py:961-985`、
  `copilot/store.py:1181-1229`、`copilot/store.py:2293-2367`。
- 根因：
  - 同 SHA 路由将所有非 `done` 状态（包括 `pending`、`running`）重置为 `pending` 并再次调度；
  - background 把通用状态 setter 当作 claim，但其 `UPDATE` 不限定旧状态，第二次
    `running -> running` 仍返回 `rowcount=1` 并递增 attempts；
  - 最终提交只校验“当前 SHA 相同”，不校验 claim 状态、attempt generation 或 claim token，
    所以两个执行者都可以新增 `BulkUpload` report 和 analysis。
- 可复现证据：隔离临时 SQLite 中并发启动两次
  `_analyze_uploaded_session_background(..., sha="sha")`，结果为
  `llm_calls=2`、两个调用均返回成功、`analyses=2`、`report_ids=[2,1]`、
  `analysis_attempts=2`。Store 级连续两次领取也均返回 1。
- 风险：网络重传、重复上传请求或运行中手动重试会产生双倍付费模型调用、重复诊断/事件，
  并把 attempts 变成“调度次数”而非可信尝试数；后续 attention 投影还可能把重复 analysis
  当成两个独立来源。
- 修正门：提供只允许预期旧状态到 `running` 的专用 CAS，并让 commit 校验同一 claim/generation；
  并发回归必须断言 LLM=1、report=1、analysis=1、event=1。

### I2. `model` provenance 记录请求配置值，而不是 provider 实际返回值

- 位置：`copilot/llm.py:382-385`、`copilot/llm.py:400-440`；错误测试期望见
  `tests/test_llm.py:400-440`。
- 根因：`analyze()` 先从配置读取 `model`，读取 provider JSON 后完全忽略响应中的
  `data["model"]`，成功、JSON 失败和无响应的 provider 失败路径都回填配置值。
- 可复现证据：fake provider 返回 `model="provider-resolved-v2"`，配置请求
  `model="requested-alias"`，实际 `AnalysisOutcome.model` 为 `requested-alias`。
  当前测试的 fake response 甚至没有 `model` 字段，却断言结果等于配置 model，正好固化了
  与实施报告“provider 未实际返回 model 时保持空”相反的语义。
- 风险：provider 对 alias 做路由、版本升级或故障切换时，持久化 trace 会声称使用了另一个模型；
  provider 尚未返回任何响应时也会伪造“实际模型”，导致诊断质量比较、审计和问题复现失真。
- 修正门：`model` 只记录响应中经过有界校验的实际值；无实际值时保持空。若仍需记录请求值，
  应另设 `requested_model`，不能复用 provenance `model`。

### I3. 未知 session 没有在 LLM 前 ask-first 原子绑定

- 位置：`copilot/service.py:1318-1365`、`copilot/store.py:708-748`、
  `copilot/store.py:1762-1803`；错误测试期望见 `tests/test_student_ask_api.py:297-343`。
- 根因：API 在调用 LLM 前执行 `ensure_session_owner()`，但该方法遇到不存在的 session row
  直接返回；真正创建最小 session/绑定 owner 的事务直到 LLM 回答完成后的
  `add_student_ask()` 才发生。
- 可复现路径：A 首先用未知 `sess-race` 发起 ask 并阻塞在 LLM；B 在此期间
  `upsert_session("sess-race", "stu-b", ...)` 成功；A 的 LLM 已经产生答案后，写 ask
  才因 owner 冲突返回 409。现有 `test_session_owner_change_during_llm_returns_conflict...`
  正是在断言该错误顺序为 GREEN。
- 风险：先到的提问者不能赢得 owner claim，付费 provider 调用被浪费，问题和回答均不落库；
  同时“API 首个 ask”与“事务首个写入者”的所有权语义不一致。
- 修正门：在构造上下文和调用 LLM 前，用 `BEGIN IMMEDIATE` 原子检查并创建/绑定最小 session；
  新并发测试应阻塞 A 的 LLM，证明 B 绑定失败而 A 最终成功持久化 ask。

## Minor

0。

## 其余合同核对

| 合同项 | 结论 | 证据摘要 |
|---|---|---|
| confidence / evidence / 迁移 | 通过 | 非数值、bool、NaN/Inf 回落；数值夹紧；证据 3×160 上界；旧库默认可重入。 |
| prompt hash、latency、attempts、错误脱敏 | 部分通过 | 生效静态 prompt hash、Stop 成功/失败/取消 trace、累计 latency 和稳定错误码成立；model provenance 与 bulk 并发 attempts 见 I1/I2。 |
| Task 2 Stop 原子性/幂等/恢复 | 通过 | report claim、三次重试、summary+analysis+done+input 清除同事务、重复 event/recovery 相邻回归未见退化。 |
| 学员问答三态与上下文 | 部分通过 | answered/degraded/failed、有界同 owner/session 上下文、已归属 session 在 LLM 前拒绝均成立；未知 session 见 I3。 |
| 反馈 | 通过 | helpful/unresolved、首次不可变、相同重投幂等、owner/404/409/422、失败可重试；未发 mentor message/event。 |
| 原生 UI | 通过 | 状态文案、有效 ask_id 才显示反馈、成功/409 后禁用、网络失败重试、旧反馈结果不覆盖新 ask。 |
| Bulk partial / stale / 双轴 | 部分通过 | 串行同 SHA 仅重试诊断、同批一成一败、stale 丢弃、transfer/analysis 双轴成立；并发同 SHA 见 I1。 |
| 导师消息原通道 | 通过 | 300 条历史、ack 响应丢失、重启、WS/REST 重复到达组合回归保持单次渲染和单次 delivered event。 |

## 验证证据

1. Task 3 六个指定测试文件加 `tests/test_store.py`：
   - `177 passed, 1 warning`。
2. Task 2 相邻 transport/coordinator/routing/upload retry：
   - `122 passed, 1 warning`。
3. 两个隔离的一次性负控：
   - 同 SHA 两个并发 background：`2` 次 LLM、`2` 条 analysis、`2` 个 report；
   - provider 实际 model 与请求 alias 不同：outcome 错写请求 alias。
4. `git diff --check 95bed8f..dd8daad`：通过。
5. 主代理补充的沙箱外、真 Chromium/loopback 全量：`657 passed, 2 failed, 1 warning`；
   两项仍为已登记的 Python 3.14 `fcntl` import probe 与 uvicorn 双 worker 短暂 health 竞态，
   未出现 Task 3 新测试失败。

本地聚焦审查解释器为 Python 3.14；未把这些结果冒充项目要求的 Python 3.13 发布门。
本审查未修改业务代码、未提交、未推送。

## 最终复审结论 — 2026-07-14

**APPROVED**

本节覆盖文档开头的历史 `NOT APPROVED` 结论；最终修复提交为
`796d974` 和 `8774436`。

- Critical：0
- Important：0
- Minor：0

最终核对：

- 终态 upload 注册在同一 `BEGIN IMMEDIATE` 中校验 parent 归属、session 范围和
  done 精确重放；路由在解析/写 raw 前注册，拒绝路径零副作用。parent done
  CAS 在同一 UPDATE 内复核 child 集合，两个竞态方向均已关闭。
- provider 在解析 `choices` 前取得 actual model；envelope 后续异常保留该值，
  HTTP/网络失败仍保持空。
- shared raw 成功 commit 会把匹配的 `failed/pending/running` child 原子投影为
  done，并返回全部 parent ID 供刷新；只产生一次 LLM 计算。
- commit 和 fail 都校验最新 `raw_id + SHA + running + generation`；不同 raw row
  generation 同值时，旧 token 仍不能写回。

复审验证：相关四文件 `118 passed` / 1 个既有 Starlette warning；
`git diff --check` 通过。复审为只读，未修改文件。
