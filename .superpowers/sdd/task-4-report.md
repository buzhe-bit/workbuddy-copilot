# Task 4 Plan C 实施报告

## 交付

- 提交信息：`feat: add diagnosis quality evaluation gate`
- 新增确定性离线评测引擎、仓库根目录 CLI、60 条脱敏合成目录、协议与人工评审文档。
- 边界：不调用 DeepSeek 或其他 provider；不伪造人工双评审；不创建 Task 5 的 attention store / API / UI；不推送远端。

## 行为合同

1. `evaluate_cases(cases, analyzer, prediction_reviews)` 固定输出 JSON 有效率、high precision / recall、normal high 误报率、人工 actionability 均分、evidence-supported 比例、稳定 failures 和 gate。reviews 通过独立 mapping/records 输入；analyzer 内嵌自评会被硬拒绝。
2. high 只认 `attention=true && priority=high`；ground truth 的 `medium` 不进入 high recall 分母，被预测为 high 时只进入 precision 分母。真实目录使用 `none|medium|high`，不再混用 boolean draft labels。
3. 预测协议对 attention / priority、原因码、证据、建议和有限 confidence 做有界验证。attention=true 至少一条证据，建议必须非空白。priority=none 允许输出原因码，使 insufficient-context 可以保留诊断原因；期望原因码非空时预测至少命中一个，期望为空时预测也必须为空。
4. insufficient-context confidence 必须不高于 0.5。fixture 预登记的 forbidden claim 命中、人工判定 evidence unsupported、原因不匹配或任一指标越界都会 fail closed。
5. case-level ground truth 与 prediction review 是两层独立人工评审。每层都要求两名审核者与一名仲裁者的 reviewer_id 三方互异；缺内容、缺评分或缺仲裁统一报告 `human_review_incomplete`。
6. prediction JSONL 禁止嵌入 `human_review`。CLI 只从单独 `--reviews` JSONL 按 id 合并；缺失、重复或额外的 prediction/review id 都产生稳定 failure。
7. 真实 60 条 fixture 恰好为 15 normal / 15 technical_stuck / 10 repeated-or-offtopic / 10 insufficient-context / 5 ask-failure / 5 system-failure。内容脱敏，三个人工字段均保持 null，因此当前真实集不会假通过。
8. CLI 默认完全离线，输出机器可读 JSON；通过退出 0，输入、人工审核或质量门失败退出 1。真 provider 只允许在获授权后由独立步骤生成 prediction 文件，不进入默认 CI。

## RED / GREEN 证据

| 阶段 | RED | GREEN |
|---|---|---|
| 最小接口 | `ModuleNotFoundError: copilot.evaluation` | 单个指标边界测试 1 passed |
| 指标与协议 | 16 failed / 1 passed：指标、零分母、无效 JSON、人工层、forbidden 与 confidence 均未实现 | 第一轮引擎 18 passed |
| CLI / 目录 | 缺 `EXPECTED_CATEGORY_COUNTS`；真实 fixture 未建立 | 60 条目录、重复/缺失 prediction 和 CLI 退出合同通过 |
| 评审分离 | 模型内嵌 `human_review` 可自评分并通过 | prediction 与 `--reviews` 分离，内嵌自评硬失败 |
| priority 语义 | 真实目录用 boolean，无法区分 medium 与 high | 全部改为 `none|medium|high`；repeated 中 02/04/06/09 为 medium；medium 指标表驱动通过 |
| insufficient 原因 | none priority 携带原因码被判 invalid；错误原因仍 gate pass | none 可带有界原因；expected reason 非空必须 overlap |
| 双评审真实性 | 仲裁者可复用审核者 ID；空 evidence 也被视为完成 | 两层三方 ID 互异，case 注释内容有界且 evidence/action 非空 |
| 首轮独立 review | C1：主 API 可信任 analyzer 自评；I3：空 evidence/action、catalog 校验不足、normal 可带任意 reason | reviews 改为第三个独立输入，内嵌自评硬失败；协议与 catalog 全边界校验；empty expected reason 强制 empty prediction reason |
| 畸形 case | `expected_reason_codes` / `forbidden_claims` 错型会直接 `TypeError` | 稳定 `case_*_invalid` failure，不崩溃 |
| 复审补充 | JSON array/object category 与 priority 触发 unhashable `TypeError` | 先验证 string 类型；list/dict 回归 4 failed → 16 passed |

## 验证

| 范围 | 结果 |
|---|---|
| `tests/test_evaluation.py` | 修复后 62 passed |
| focused + 相邻 AnalysisResult / LLM | 131 passed |
| 非 e2e 全量（Python 3.14 诊断） | 707 passed / 2 个已登记基线 failed / 1 warning |
| `compileall` / `git diff --check` | PASS |
| 独立 review | 首轮 C1 / I3 / M0；复审补充 I1；最终实现快照 `8981432` 为 C0 / I0 / M0，Approved |

本机为 Python 3.14.4，而项目发布合同是 `>=3.13,<3.14`，因此这里只记录诊断证据，不冒充 Python 3.13 发布门。非 e2e 两项失败仍是已登记基线：student-core 导入探针在 3.14 看到 `fcntl`，以及 uvicorn 双 worker 在 supervisor 收口前短暂提供 `/health`。真实集人工双标注仍是外部 gate，当前实现只保证未完成时明确失败。

最终独立 reviewer 已确认：主 API 的 review trust boundary、协议边界、catalog 畸形输入、原因码规则和真实 fixture fail-closed 均符合合同；Critical 0 / Important 0 / Minor 0。reviewer 未修改工作树，也未重复运行测试。
