# Task 3 Plan B2 实施报告

## 交付

- 提交信息：`feat: make diagnosis and student asks traceable`
- 范围：诊断质量字段与调用 trace、Stop/Bulk 重试可追溯、学员问答状态、首次反馈、
  原生浮标状态与反馈、导师消息/全文上传原闭环组合回归。
- 边界：未创建 `attention_items`、导师雷达或 Task 5+ 投影；未改变导师消息 receipt
  语义；未推送远端。

## 行为合同

1. `AnalysisResult` 增加 `confidence/evidence/model/prompt_hash/latency_ms`。confidence
   对非数值、布尔、NaN/Inf 回落 0.5，有限数值夹紧到 `[0,1]`；evidence 只保留最多
   3 条非空字符串，每条最多 160 字。旧库默认值可重入迁移。
2. 诊断 prompt 明确要求证据约束和禁止编造。`prompt_hash` 只覆盖实际生效的静态系统协议
   与数据库覆盖后的过程提醒，不含学生动态对话；SHA-256 为 64 位十六进制。
3. Stop 分析成功与失败都记录可信 model、prompt hash、单次/累计耗时和 attempt count。
   provider 未实际返回 model 时保持空；内建本地降级明确记为 `fallback`。取消在 claim 后
   写 `analysis_cancelled` 与 trace，再原样重抛。
4. Bulk raw 同样记录 model/hash/累计 latency/attempts。running 对目标 SHA 使用
   `BEGIN IMMEDIATE` 原子领取并递增；失败只写稳定错误和本次耗时；同 SHA 重试成功保留
   累计值。目标 SHA 已消失时不调用 LLM；旧结果只按目标 SHA 更新，不污染最新全文。
5. 学员问答返回 `answered/degraded/failed` 和 `needs_attention`。禁用/缺配置为 degraded；
   已配置 provider 的超时、HTTP、结构非法或空回答为 failed。错误码仅允许 1–80 位小写
   snake-case；非法 custom outcome 安全回落，不把异常正文、provider body 或密钥写入库/日志。
6. 提问只读取同一学员当前 session 的最近 16 条消息。已归属其他学员的 session 在 LLM
   前 409 且零副作用；未知 session 在 ask 写入事务中创建最小 owner 绑定，阻止稍后被其他
   学员认领。LLM 期间 owner 发生变化时也以 409 收口，不写 ask 或事件。
7. `student_asks` 增加 answer 状态和反馈字段。反馈只允许 `helpful/unresolved`，note trim 后
   最多 500 字；首次写入不可变。完全相同重投 200/`updated=false`，不同重投 409，错 owner
   403，缺失 404。反馈不发导师消息或 mentor event。
8. 原生浮标显示回答状态；只有 `ask_id>0` 时显示“有帮助/未解决”。提交成功或 409 已记录后
   禁用按钮；失败可重试。异步反馈携带 ask_id，旧请求结果不能覆盖后来问题。
9. 导师消息新增组合系统回归：299 条已确认历史 + 1 条待确认，在同一 payload 经 WS/REST
   重复到达、服务端已 ack 但响应丢失、进程重启再重放后，UI 只渲染一次，SQLite 保持
   300 个唯一 message_id，导师只收到一次 delivered 事件。
10. 上传回归覆盖同 SHA 仅重试分析、同批一成一败、stale 结果丢弃和 raw trace；成功 child
    不因同批另一 child 失败而丢失，父请求在 transfer 完成后汇总为 failed。

## RED / GREEN 证据

| 阶段 | RED | GREEN |
|---|---|---|
| 诊断字段 | JSON 无 confidence/evidence；越界/非法值直入 | 默认/夹紧/有界化通过，旧库迁移默认兼容 |
| Stop trace | analysis/report 无 model/hash/latency/attempt | 成功、三次失败、取消均持久 trace；异常正文不泄漏 |
| 问答 outcome | fallback 字符串掩盖禁用与 provider 失败 | 三态 + 稳定错误码；非法 status/answer/error code fail closed |
| session 隔离 | A 可把 ask 记到 B 的 session；未知 session 可被 B 后认领 | 已知冲突在 LLM 前 409；写入事务原子绑定；竞态后写 409 |
| feedback | 无字段/接口/owner 保护 | 首写不可变、幂等重投、403/404/409/422 与 500 字边界通过 |
| 浮标 | 只显示答案，无状态/反馈 | 三态文本、两反馈按钮、409 已记录、旧异步结果隔离通过 |
| Bulk trace | raw 只有 status/error；重试不可解释 | attempts/累计 latency/model/hash 入 raw/report/analysis，SHA 原子领取 |
| 原消息闭环 | 大历史、重复、丢 ack、重启分别测试 | 新增 300 条历史的单一组合系统回归，渲染/receipt 均幂等 |

## 验证

| 命令 | 结果 |
|---|---|
| Task 3 六个指定测试文件 | 146 passed / 1 个既有 Starlette warning |
| 指定文件 + `tests/test_store.py` | 177 passed / 1 个既有 Starlette warning |
| 排除端口型 `tests/e2e`、3.14 platform 探针和单 worker 端口用例 | 616 passed / 1 个既有 warning |
| 修正 fixture 后排除 `tests/e2e` 的独立复跑 | 628 passed / 2 个已登记失败，无 Task 3 新失败 |
| 完整 `pytest -q`（当前沙箱） | 627 passed / 3 failed / 29 errors；其中 1 个旧 Store fixture 已随后修正并在 177 项回归中转绿 |
| `git diff --check` | PASS（最终提交前复跑） |

完整命令中的 29 个 error 均为当前沙箱禁止绑定 `127.0.0.1`：mentor UI 25 项、
student agent system 4 项。另两项非 Task 3 基线分别是 Python 3.14 导入树含 `fcntl`，以及
单 worker 真进程测试同样无法绑定 loopback。旧 Store fixture 曾让 Bob 复用 Alice 的
`sess-1`；按本任务明确的全局 session owner 不变量改为 Bob 独立 session，未放宽生产防线。
fixture 修正后的独立非 e2e 复跑为 628 passed，只剩上述两项已登记失败。

本机仍为 Python 3.14，项目发布合同要求 `>=3.13,<3.14`；因此上述结果是 3.14 诊断与
当前沙箱可执行证据，不冒充 Python 3.13 + 真浏览器发布门。
