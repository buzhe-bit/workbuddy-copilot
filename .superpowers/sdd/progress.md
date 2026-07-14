# WorkBuddy Copilot SDD Progress

Plan: `docs/superpowers/plans/2026-07-14-diagnosis-attention-pilot.md`
Merge base: `41a37bec5f74b805f154ee0d797ba0e864cd9608`
Controller plan commit: `31de321`

Environment gates:
- Local Python 3.13: unavailable; CI configuration and final gate must remain pending until a 3.13 runtime runs it.
- Human dual annotation: cannot be claimed by agents; evaluation CLI must fail closed until completed.
- Seven-day pilot / macOS and Windows real-machine gates: external manual evidence required. Windows code may reach `implementation_candidate` in hosted CI, but `rollout_ready` remains blocked until W0/W1 evidence exists.
- Paid provider cost comparison: not authorized; Task 13 defaults to deterministic offline experiments and must label paid comparisons pending.
- External experiment mirror: write only to `/Users/xiaoshushenxia/Documents/测试/🏭项目/超脑ai夏令营/黑客松/`; never delete existing files there.

Tasks:
- Task 1: completed and independently APPROVED (`ce6c777`, fix `7481a5d`); local 3.14 diagnostic 553 passed / 2 known failures, Python 3.13 release lane remains external.
- Task 2: completed and independently APPROVED (`741bc82`, fixes `e5e656a`, `95bed8f`); final local 3.14 full regression 602 passed / 2 known baseline failures / 1 warning, Python 3.13 release lane remains external.
- Task 3: completed and independently APPROVED (`dd8daad`, fixes `796d974`, `8774436`); final local Python 3.14 full browser regression 674 passed / 2 known baseline failures / 1 warning, Python 3.13 release lane remains external.
- Task 4: completed and independently APPROVED (`e8f2a4a`); final local Python 3.14 diagnostic: focused 62 passed, focused + adjacent 131 passed, non-e2e 707 passed / 2 known baseline failures / 1 warning; Python 3.13 and human dual annotation gates remain external.
- Task 5: implementation complete and independently APPROVED (C0/I0/M0), focused 83 passed and adjacent 279 passed; no commit yet. Local Python 3.14 non-browser diagnostic 790 passed / 2 known baseline failures / 1 warning; Python 3.13 release lane remains external.
- Task 6: pending
- Task 7: pending
- Task 8: pending
- Task 9: pending — Windows WorkBuddy/runtime/Stop transcript durability and hosted Windows system lane.
- Task 10: pending — Windows floating UI, installer lifecycle, evidence tooling and first-class client candidate.
- Task 11: pending — dual-platform pilot/release gates; Windows rollout remains externally blocked without W0/W1.
- Task 12: pending — 10/50/100/300 student scale, failure and soak gates.
- Task 13: pending — at least 25 pre-registered cost experiments plus external mirror.

Current branch: `codex/workbuddy-d-attention-backend`
