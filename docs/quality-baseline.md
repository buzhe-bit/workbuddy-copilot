# WorkBuddy Copilot 质量基线与发布门

## 自动化基线

| 车道 | 运行环境 | 发布证据 |
|---|---|---|
| Linux server/core | Python 3.13 | 非真机 server/core 测试、机器可读摘要、共享生产逻辑分支覆盖率 `>=80%` |
| macOS client/browser | Python 3.13 + 真 Chromium | 平台导入、导师台 Playwright、Student Agent 系统 E2E |
| Windows hosted runtime | Python 3.13 + PowerShell | Windows runtime/UI/installer/evidence 与真 loopback component 测试；不等于 W1 真机通过 |
| Windows W1 evidence | 受信任真机/自托管 runner | schema、artifact hash、commit/build/runner 一致且所有人工门通过；缺证据必须返回 blocked |

CI 产物 `quality-*.json`、pytest 输出、`coverage.json` 和 Windows evidence 是权威结果；README 不手写“通过测试数”。

## 覆盖率口径

- `pyproject.toml` 开启 branch coverage，`fail_under=80`。
- Linux 门覆盖 Controller–Service–Repository、EventBus、Student Core 和共享业务逻辑。
- macOS PyObjC、Windows Tk/Win32 等原生 UI 适配不用 Linux 伪执行提高数字，而是由独立平台、真浏览器和 W1 门验证。
- 覆盖率 80% 只是最低结构门，不替代 60 条诊断质量集、幂等/恢复、身份隔离、真机 UI 和试点浸泡。

## 开发者验证

```bash
python3.13 -m venv venv
venv/bin/python scripts/python_preflight.py
venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
venv/bin/python -m pytest -q
git diff --check
```

与 Linux CI 相同的共享生产逻辑覆盖率命令见 `.github/workflows/quality.yml`。本地使用非 Python 3.13、沙箱禁止 loopback 端口或缺原生依赖时，必须在验证记录中单独列为环境限制，不把 skip/无法运行写成 pass。

## 发布停止条件

以下任一条为真即不得发布：

- Python 3.13 前置检查失败、全量测试失败或共享逻辑覆盖率低于 80%；
- critical test skip，或 Playwright 三个目标 viewport 未执行；
- 同一 `event_id` 重发产生重复 report/prompt/analysis；
- 202 后重启丢失诊断、消息假送达、同 SHA 重复传输、跨学员读写或学习关注推送到学员端；
- Windows W0/W1 证据缺失、过期或与当前 commit/build 不匹配；
- 没有可验证备份、回滚命令和当班处理人。

## 当前外部门

- Windows WorkBuddy W0/W1 真机取证：`BLOCKED: real-machine evidence missing`。
- macOS 原生浮标 P3 实机冒烟：待执行。
- 60 条诊断集的两名人工评审/冲突裁决：待业务评审。
- 3–5 学员、1–2 导师7天试点：未运行。
