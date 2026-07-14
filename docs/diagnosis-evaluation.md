# 诊断质量评测门禁

这套门禁只做确定性、离线评测，不调用模型、网络或付费 provider。它回答的是：一批已经生成的诊断预测，在高优先级识别、证据真实性和建议可执行性上是否达到发布线。

## 当前状态

`tests/fixtures/evaluation/diagnosis_cases.jsonl` 固定为 60 条脱敏合成样本：

| 类别 | 数量 | draft priority |
|---|---:|---|
| `normal` | 15 | `none` |
| `technical_stuck` | 15 | `high` |
| `repeated_or_offtopic` | 10 | 6 条 `high`、4 条 `medium` |
| `insufficient_context` | 10 | `none` |
| `ask_failure` | 5 | `high` |
| `system_failure` | 5 | `high` |

这些 priority 和原因码只是 draft labels，不是已确认 ground truth。真实 fixture 的 `reviewer_1`、`reviewer_2`、`adjudicated` 目前全部为 `null`，因此用它运行 CLI 必须得到非零退出码和 `human_review_incomplete`。不得由模型或代理伪造两名人工审核完成。

## 两层独立人工评审

门禁要求两层证据，缺任一层都 fail closed：

1. case-level ground-truth review：两名人工审核者分别标注 `expected_attention`、`expected_reason_codes`、`evidence`、`acceptable_actions` 和 `forbidden_claims`，再由第三个不同 reviewer_id 完成 adjudication。顶层 draft 字段只有在 adjudicated 内容与它一致时才成为评测依据。
2. prediction review：两名人工审核者分别给模型输出的 actionability 打 1–5 分，并判断 `evidence_supported`；第三个不同 reviewer_id 完成 adjudication。最终指标只读取 adjudicated 值。

两层审核不能用 `reviewed: true` 等状态位代替。三方 reviewer_id 必须互异，case-level 证据与可接受动作不能为空。预测评分不能由模型自评，也不能嵌入 prediction JSONL。

推荐流程是：冻结样本内容 → 两人独立标注 case → 仲裁并同步顶层 ground truth → 单独生成模型 predictions → 两人独立审核 predictions → 仲裁 → 运行门禁。审核者应先独立提交，仲裁前不相互覆盖原始标注。

## 三种 JSONL 输入

每行必须是一个 JSON object，`id` 在各文件中一一对应且不能重复。

Case 文件：

```json
{"id":"technical-stuck-01","category":"technical_stuck","transcript":"脱敏对话","latest_prompt":"下一步怎么查？","expected_attention":"high","expected_reason_codes":["technical_stuck"],"acceptable_actions":["比较期望值与实际值"],"forbidden_claims":["数据库连接失败"],"reviewer_1":{"reviewer_id":"gt-r1","expected_attention":"high","expected_reason_codes":["technical_stuck"],"evidence":["对话中的原文证据"],"acceptable_actions":["比较期望值与实际值"],"forbidden_claims":["数据库连接失败"]},"reviewer_2":{"reviewer_id":"gt-r2","expected_attention":"high","expected_reason_codes":["technical_stuck"],"evidence":["对话中的原文证据"],"acceptable_actions":["比较期望值与实际值"],"forbidden_claims":["数据库连接失败"]},"adjudicated":{"reviewer_id":"gt-adjudicator","expected_attention":"high","expected_reason_codes":["technical_stuck"],"evidence":["对话中的原文证据"],"acceptable_actions":["比较期望值与实际值"],"forbidden_claims":["数据库连接失败"]}}
```

`expected_attention` 使用 `none|medium|high`。`medium` 是需要提醒但不进入 high 召回分母的样本；若把 medium 预测为 high，会进入 high precision 的分母但不是 true positive。`insufficient_context` 使用 `none`，同时仍应输出 `insufficient_context` 原因码和不高于 0.5 的 confidence。

Case 目录只接受上例列出的顶层字段，不接受额外字段。`transcript` 必须是 1–20,000 字符的非空字符串，`latest_prompt` 必须是 1–2,000 字符的非空字符串；expected reason codes 最多 5 个，acceptable actions 为 1–5 条且每条不超过 500 字符，forbidden claims 最多 10 条且每条不超过 160 字符。

Prediction 文件只包含模型字段，不能出现 `human_review`：

```json
{"id":"technical-stuck-01","prediction":{"attention":true,"priority":"high","reason_codes":["technical_stuck"],"evidence":["对话中的原文证据"],"suggested_action":"比较期望值与实际值","confidence":0.91}}
```

协议边界：

- `attention` 必须是 boolean，且仅可与 `priority=none|medium|high` 一致组合；`none` 对应 false，`medium/high` 对应 true。
- `reason_codes` 最多 5 个、去重、每个不超过 80 字符且为小写数字下划线格式。priority 为 none 时仍可报告原因码；case 的 expected reason codes 非空时，预测至少命中一个，expected 为空时预测也必须为空。
- `evidence` 最多 3 条，每条不超过 160 字符；attention=true 时至少一条。`suggested_action` 必须是非空白字符串且不超过 500 字符。
- `confidence` 必须是 0–1 的有限数字。

Review 文件独立保存人工 prediction review：

```json
{"id":"technical-stuck-01","reviewer_1":{"reviewer_id":"pred-r1","actionability":4,"evidence_supported":true},"reviewer_2":{"reviewer_id":"pred-r2","actionability":5,"evidence_supported":true},"adjudicated":{"reviewer_id":"pred-adjudicator","actionability":4,"evidence_supported":true}}
```

程序内接口同样不信任 analyzer 自评：`evaluate_cases(cases, analyzer, prediction_reviews)` 的第三个参数接收独立的 `{case_id: review}` mapping 或带 `id` 的 review records。analyzer 返回值只允许预测协议字段；出现 `human_review` 会产生 `prediction_review_embedded:<id>`，即使同时传入合法独立 review 也不能通过。

## 门槛与失败语义

| 指标 | 发布门槛 |
|---|---:|
| JSON / 协议有效率 | ≥ 99% |
| high precision | ≥ 80% |
| high recall | ≥ 90% |
| normal 样本 high 误报率 | ≤ 5% |
| 人工 actionability 仲裁均分 | ≥ 4/5 |
| 人工 evidence supported 比例 | 100% |

任何 forbidden claim 命中、人工判定证据不受支持、期望原因码不相交、insufficient-context confidence 超过 0.5、缺失/重复预测、无效 JSON 或任一人工评审未完成，都会加入 `failures` 并让 `gate_passed=false`。precision 或 recall 分母为零时按 0 计算并失败，不用空集合制造通过结果。

证据真实性由人工审核结合原对话判断，不能用关键词规则替代。`forbidden_claims` 只负责拦截样本预先登记的明确幻觉，例如不存在的文件、错误、命令结果或学习状态。

## 离线运行

在仓库根目录执行：

```bash
python scripts/evaluate_diagnosis.py \
  --cases tests/fixtures/evaluation/diagnosis_cases.jsonl \
  --predictions artifacts/diagnosis-predictions.jsonl \
  --reviews artifacts/diagnosis-reviews.jsonl \
  --output artifacts/diagnosis-evaluation.json
```

通过时退出码为 0；任何门槛或输入合同失败时退出码为 1。标准输出和 `--output` 都是同一份机器可读 JSON 报告。CLI 不读取 API key，也不发网络请求。

真实 DeepSeek 发布冒烟不进入默认 CI。只有获得付费调用授权后，才可由独立生成步骤产出不含人工评分的 prediction JSONL；随后仍使用上面的离线命令，并从独立 review JSONL 合并人工评分。当前仓库没有授权、也不会自动发起这类调用。
