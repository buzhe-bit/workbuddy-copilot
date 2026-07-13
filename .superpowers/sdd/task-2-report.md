# Task 2 Plan B1 实施报告

## 交付

- 提交信息：`feat: make hook analysis delivery durable`
- 范围：Hook `event_id` 端到端幂等、Stop 有界输入持久化、有界重试与受管启动恢复、
  UserPrompt crash-window 补偿、旧库幂等迁移与 legacy raw 恢复。
- 边界：未扩展 Task 3+；未推送远端；未改动 Task 1 已登记的平台/多 worker 基线。

## 行为合同

1. 新客户端将 spool entry id 作为 `event_id` 透传到 `/report`；旧客户端不传时仍每次接收。
2. 非空 `(student_id, event_id)` 唯一。重投返回原 `report_id`、`analysis_status`和
   `duplicate=true`；跨事件类型冲突返回 409。HTTP 与 Store 共用 ID 规则，HTTP 类型
   兼容 Pydantic 1/2。
3. Stop 接收事务同时落 report/session/显式全文/最多 256 KiB 的本次分析输入。
   tail 优先，仅 tail 为空时使用同次显式 full；绝不借用后来无关全文。
4. 分析最多尝试 3 次，延迟 0/1/5 秒。SQLite 原子 claim 保证并发恢复不重复调 LLM；
   尝试数、状态、稳定错误码和下次时间都持久化。成功后原子写 analysis 并清除输入；
   失败保留输入。
5. 启动 readiness 前只同步修复 running 和枚举 report ID；provider drain 使用单一受管
   background task。退出先 cancel + await，后释放 worker lock；意外 task 异常在 shutdown
   重新抛出。准备失败时 flag 保持 false，同 Context 可重试。
6. 旧库 `analysis_input=NULL` 且带 explicit-raw marker 时，只允许同学员、同会话、
   `content_sha256 IS NULL`、0–5 秒内且 report↔raw 双向唯一的显式全文。反向
   竞争统计窗口内所有 explicit-marker Stop，不因现代 report 已有 durable input 而
   忽略它。无法证明唯一归属时终态失败为 `analysis_input_unavailable`，不调 LLM。
7. UserPrompt 的 prompt 以 `report_id` 幂等创建；如 report 已落库而 prompt 未落库，重投使用
   原 report 内容补齐。
8. legacy prompt 迁移只考虑 `event_id IS NULL`、report prompt 非空、内容精确相同、
   prompt 不早于 report 且延迟不超过 5 秒的双向唯一配对。空 prompt、现代
   `event_id`、逆序、超窗或歧义均不回填。
9. legacy `sessions.student_id` 为 `NULL/''` 时，第一个合法写入者在
   `BEGIN IMMEDIATE` 事务内 CAS 绑定 owner；后续其他学员明确失败且整个写入
   回滚。不从旧 report/raw/prompt 子表猜测 owner。

## RED / GREEN 证据

| 阶段 | RED | GREEN |
|---|---|---|
| HTTP 幂等 | 同 `event_id` 重投 10 次产生 10 条 report/prompt/analysis | 10 次均 202，1 条 report/prompt/analysis，1 次 LLM |
| 202 后重启 | 普通 Stop tail 在新 Store 实例中消失 | 同 SQLite 重启后用当次持久输入恢复，成功后清除 |
| 传递链 | transport 无 `event_id` 参数，coordinator/consumer 丢 id | 两条消费路径均将 entry id 透传到 HTTP body |
| 重试 | 无 wrapper/无 claim/无稳定状态 | 第三次成功观察 0/1 延迟；持续失败最终 3 次，延迟序列为 0/1/5 |
| 并发/恢复 | 重复 recovery 可同时调用 provider | 原子 claim 下只有 1 次 provider 调用；超限项不再重放 |
| 启动生命周期 | 慢 provider 阻塞 lifespan；准备异常后 flag 已置位 | provider 未完成时 `/health` 200；shutdown 取消并 await；准备失败可重试 |
| legacy 输入 | recovery 虽读 raw，wrapper 仍从 NULL `analysis_input` 分析空内容 | raw 先有界持久，provider 收到对应内容 |
| 迁移/隐私 | 旧 pending 行保留错误状态，provider 秘密可进入异常/日志 | 迁移重入并正确回填；只持久和抛出稳定 snake-case 错误码 |
| 审查边界 | 冲突 event 可错误触发 UserPrompt 副作用 | 冲突 event 返回 409；崩溃窗口中缺失的 prompt 可幂等补齐 |

## 验证

| 命令 | 结果 |
|---|---|
| Task 2 七个相关测试文件 | 153 passed / 1 个既有 Starlette warning |
| loopback `NO_PROXY` + 真 Chromium 下全量 `pytest -q` | 586 passed / 2 failed / 1 warning |
| `python scripts/python_preflight.py` | 预期 exit 1：Python 3.14.4 不满足 `>=3.13,<3.14` |

全量两项失败与 Task 1 已登记基线完全一致：

- `tests/test_platform_imports.py::test_student_core_import_tree_is_platform_neutral`：Python 3.14
  运行时导入树含 `fcntl`。
- `tests/test_single_worker_startup.py::test_uvicorn_cli_workers_two_fails_closed_before_serving_health`：
  supervisor 终止前一个 worker 可短暂响应 `/health`。

本机无 Python 3.13，因此本地 3.13 门仍为 **BLOCKED: interpreter unavailable**；上述
3.14 结果只是诊断，不冒充发布合同全绿。

## 独立 Review 追加修复

- 基础交付提交：`741bc820116bf38fbcd6957a77edb8a831436930`。
- 追加提交信息：`fix: reschedule recoverable duplicate stop reports`。
- RED 1：report 已落 pending，但首次 BackgroundTask 未启动时，duplicate 重投不调用
  provider；近同时用例等不到 wrapper。
- RED 2：一律调度 duplicate 后，running/done/failed attempts=3 也会进 wrapper；状态门
  4 failed / 1 passed。
- RED 3：lifespan 取消已 claim 的 recovery 后，report 停在 running，同进程无法重投。
- GREEN：只有 pending 或 attempts < 3 的 failed duplicate 再排队；两请求在 claim 前同时
  入 wrapper 仍仅 1 provider / 1 analysis；done 重投不再入 wrapper。claim 后取消会 CAS
  回 failed，保留 input/attempts，写 `analysis_cancelled` 并原样重抛。
- 追加聚焦：7 passed；Task 2 七文件：153 passed；最终全量：586 passed / 2 个已登记
  baseline failed / 1 warning。

## 独立 Review 二轮追加修复

- legacy raw 先用 5 类过时/歧义/交叉/缺失场景证明旧查询会错配；随后又分别
  复现带 SHA 的批量 raw 与现代 durable Stop 所属 raw 被 legacy report 借用。
  修复后只有可证明的唯一即时 raw 可被 CAS 持久，其余均 fail closed。
- legacy blank session owner 的 `NULL` 和空字符串均已覆盖：顺序 accept、
  `upsert_session` 与双连接双线程竞争共 6 个用例。竞争结果严格为 1 个
  success + 1 个 `ValueError`，无 SQLite lock 暴露，输家无 report/raw/prompt 副作用。
- prompt 迁移先复现了“古老 prompt 绑到现代 event report”和“空 Stop prompt
  认领任意近邻 UserPrompt”。修复后恢复过程不会用无关 prompt 污染 LLM
  `latest_prompt` 或 AI summary 归属。
- 聚焦验证：Store/Service/迁移三文件 102 passed；Task 2 七文件
  168 passed；均只有 1 个既有 Starlette 弃用 warning。
