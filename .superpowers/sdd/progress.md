# WorkBuddy Copilot SDD Progress

Plan: `docs/superpowers/plans/2026-07-14-diagnosis-attention-pilot.md`
Merge base: `41a37bec5f74b805f154ee0d797ba0e864cd9608`
Controller plan commit: `31de321`

Environment gates:
- Local Python 3.13: unavailable; CI configuration and final gate must remain pending until a 3.13 runtime runs it.
- Human dual annotation: cannot be claimed by agents; evaluation CLI must fail closed until completed.
- Seven-day pilot / macOS real-machine gate: external manual evidence required.
- Paid provider cost comparison: not authorized; Task 11 defaults to deterministic offline experiments and must label paid comparisons pending.
- External experiment mirror: write only to `/Users/xiaoshushenxia/Documents/测试/🏭项目/超脑ai夏令营/黑客松/`; never delete existing files there.

Tasks:
- Task 1: completed and independently APPROVED (`ce6c777`, fix `7481a5d`); local 3.14 diagnostic 553 passed / 2 known failures, Python 3.13 release lane remains external.
- Task 2: completed and independently APPROVED (`741bc82`, fixes `e5e656a`, `95bed8f`); final local 3.14 full regression 602 passed / 2 known baseline failures / 1 warning, Python 3.13 release lane remains external.
- Task 3: completed and independently APPROVED (`dd8daad`, fixes `796d974`, `8774436`); final local Python 3.14 non-e2e regression 645 passed / 2 known baseline failures / 1 warning, Python 3.13 release lane remains external.
- Task 4: pending
- Task 5: pending
- Task 6: pending
- Task 7: pending
- Task 8: pending
- Task 9: pending
- Task 10: pending — 10/50/100/300 student scale, failure and soak gates.
- Task 11: pending — at least 25 pre-registered cost experiments plus external mirror.

Current branch: `codex/workbuddy-b-loop-hardening`
