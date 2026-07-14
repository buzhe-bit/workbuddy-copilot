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
- Task 5: completed and independently APPROVED (`d7ed73c`, C0/I0/M0); focused 83 passed and adjacent 279 passed. Local Python 3.14 non-browser diagnostic 790 passed / 2 known baseline failures / 1 warning; Python 3.13 release lane remains external.
- Task 6: completed and independently APPROVED (`b9f9e52`, C0/I0/M0); mentor UI 72 passed, adjacent backend 149 passed, real Student Agent system 4 passed. Local Python 3.14 non-mentor-UI diagnostic 804 passed / 2 known baseline failures / 1 warning; Python 3.13 release lane remains external.
- Task 7: completed and independently APPROVED (`cfeae49`, C0/I0/M0); responsive Chromium 8 passed, mentor UI + static 80 passed. Local Python 3.14 non-mentor diagnostic remains 752 passed / 2 known baseline failures after excluding unavailable PyObjC native collection; Python 3.13 release lane remains external.
- Task 8: completed and independently APPROVED (`740e0c1`, fixes `2e95dd5`, C0/I0/M0); focused + adjacent 357 passed, broader non-e2e 823 passed / 3 known environment gates deselected / 2 warnings. Python 3.13, real Chromium/loopback and Windows W0/W1 remain external gates.
- Task 9: completed and independently reviewed (`92e405d`, P0=0/P1=0; final JUnit 1110 tests / 0 failures / 0 errors / 3 Windows real-machine skips). Two P2 follow-ups remain: used-order tombstone compaction/health and pre-publication owner residue.
- Task 10: in progress — Windows floating UI, installer lifecycle, evidence tooling and first-class client candidate.
- Task 11: pending — dual-platform pilot/release gates; Windows rollout remains externally blocked without W0/W1.
- Task 12: pending — 10/50/100/300 student scale, failure and soak gates.
- Task 13: pending — at least 25 pre-registered cost experiments plus external mirror.

Current branch: `codex/workbuddy-e2-windows-runtime`
