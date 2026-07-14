# -*- coding: utf-8 -*-
"""导师观察台前端 / 视觉 Playwright 测试（route-mock 版）。

策略
----
- **不启后端**：用 Playwright `page.route` 拦截 `/api/mentor/*`，`page.route_web_socket`
  拦截 `/ws/mentor`，喂确定性 fixture，驱动真实 `app.js` 渲染。快且确定。
- 静态资源（index.html / app.js / style.css）用本机临时 `http.server` 起在随机端口，
  以便 `fetch('/api/mentor/...')` 相对路径解析到 http:// 源、被 route 拦截。
- 断言的是**内容 / 颜色 / 行为**（display_name 文本、状态点 class、精确 rgb、
  点击后其他会话状态不被刷新），不是"页面非空"。

依赖
----
- `playwright`（已装，1.61）+ chromium 内核。**不依赖** `pytest-playwright`，
  直接用 `playwright.sync_api`（含 `expect` 断言，随核心包发布）。
- 运行：`venv/bin/python -m pytest tests/e2e/test_mentor_ui.py -v`
  若 chromium 内核缺失，用例会以清晰原因 skip（见 `browser` fixture）。
"""

import functools
import http.server
import json
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest

playwright_sync = pytest.importorskip(
    "playwright.sync_api",
    reason="playwright 未安装；`venv/bin/python -m pip install playwright`",
)
from playwright.sync_api import expect, sync_playwright  # noqa: E402

# repo_root/copilot/static/mentor —— 被测前端静态资源目录
MENTOR_DIR = Path(__file__).resolve().parents[2] / "copilot" / "static" / "mentor"

# 设计令牌（frontend-spec.md）→ 期望 computed rgb。
# 断言精确 rgb：改错任一档，对应用例必红（负控见各用例注释）。
HEX_TO_RGB = {
    "#378ADD": "rgb(55, 138, 221)",   # 学员提问 蓝 badge-q
    "#534AB7": "rgb(83, 74, 183)",    # AI 回复摘要 紫 badge-ai
    "#EF9F27": "rgb(239, 159, 39)",   # 学习诊断 橙 badge-an
    "#DB2777": "rgb(219, 39, 119)",   # 导师提示 粉 badge-me
    "#ef4444": "rgb(239, 68, 68)",    # 状态点 red
    "#f59e0b": "rgb(245, 158, 11)",   # 状态点 yellow
    "#22c55e": "rgb(34, 197, 94)",    # 状态点 green
}

XSS_PAYLOAD = '<img src=x onerror="window.__xss=1">'


# ─────────────────────────────────────────────────────────────
# 本机静态资源服务（会话级）
# ─────────────────────────────────────────────────────────────
class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # 静音，避免污染测试输出
        pass


@pytest.fixture(scope="session")
def static_server():
    if not MENTOR_DIR.exists():
        pytest.skip(f"前端目录不存在: {MENTOR_DIR}")
    handler = functools.partial(_QuietHandler, directory=str(MENTOR_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # 内核缺失等
            pytest.skip(
                "无法启动 chromium 内核，请先运行 "
                "`venv/bin/python -m playwright install chromium`。原始错误: "
                f"{e}"
            )
        try:
            yield b
        finally:
            b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    pg = ctx.new_page()
    # 收集页面报错（如 XSS 脚本注入执行会在此暴露）
    pg._console_errors = []
    pg.on("pageerror", lambda exc: pg._console_errors.append(str(exc)))
    try:
        yield pg
    finally:
        ctx.close()


# ─────────────────────────────────────────────────────────────
# route mock helpers
# ─────────────────────────────────────────────────────────────
def install_api_routes(
    page,
    students=None,
    sessions_by_student=None,
    timeline_by_session=None,
    transcript_by_session=None,
    transcript_status=200,
    reply_by_prompt=None,
    reply_by_ref=None,
    reply_status=200,
    upload_statuses=None,
    upload_retry_statuses=None,
    upload_requests=None,
    attention_items=None,
    attention_status=200,
    attention_patch_status=200,
    attention_patch_hook=None,
    attention_patches=None,
    mentor_messages=None,
    mentor_message_statuses=None,
    mentor_status_requests=None,
):
    """拦截 /api/mentor/* 返回确定性 JSON。"""
    students = students or []
    sessions_by_student = sessions_by_student or {}
    timeline_by_session = timeline_by_session or {}
    transcript_by_session = transcript_by_session or {}
    reply_by_prompt = reply_by_prompt or {}
    reply_by_ref = reply_by_ref or {}
    upload_statuses = upload_statuses if upload_statuses is not None else {}
    upload_retry_statuses = upload_retry_statuses if upload_retry_statuses is not None else {}
    upload_requests = upload_requests if upload_requests is not None else []
    attention_items = attention_items if attention_items is not None else []
    attention_patches = attention_patches if attention_patches is not None else []
    mentor_messages = mentor_messages if mentor_messages is not None else []
    mentor_message_statuses = (
        mentor_message_statuses if mentor_message_statuses is not None else {}
    )
    mentor_status_requests = (
        mentor_status_requests if mentor_status_requests is not None else []
    )

    def handler(route):
        path = urlparse(route.request.url).path
        if path == "/api/mentor/attention":
            if attention_status != 200:
                route.fulfill(status=attention_status, json={"detail": "attention failed"})
            else:
                query = parse_qs(urlparse(route.request.url).query)
                rows = list(attention_items)
                for key in ("status", "priority", "category", "student_id"):
                    expected = query.get(key, [None])[0]
                    if expected is not None:
                        rows = [row for row in rows if str(row.get(key, "")) == expected]
                rows.sort(key=lambda row: (
                    0 if row.get("priority") == "high" else 1,
                    float(row.get("created_at") or 0),
                    int(row.get("id") or 0),
                ))
                limit = int(query.get("limit", [100])[0])
                route.fulfill(json={"items": rows[:limit]})
        elif path.startswith("/api/mentor/attention/"):
            item_id = int(path.rsplit("/", 1)[-1])
            payload = json.loads(route.request.post_data or "{}")
            attention_patches.append({
                "id": item_id,
                "method": route.request.method,
                **payload,
            })
            item = next((row for row in attention_items if int(row["id"]) == item_id), None)
            if route.request.method != "PATCH":
                route.fulfill(status=405, json={"detail": "PATCH required"})
            elif not str(payload.get("mentor_id", "")).strip():
                route.fulfill(status=422, json={"detail": "mentor_id required"})
            elif attention_patch_status != 200:
                route.fulfill(status=attention_patch_status, json={"detail": "patch failed"})
            elif item is None:
                route.fulfill(status=404, json={"detail": "not found"})
            else:
                previous_status = item.get("status", "open")
                item.update({
                    "status": payload.get("status", item.get("status", "open")),
                    "handled_by": payload.get("mentor_id", ""),
                    "resolution_note": payload.get("note", ""),
                    "updated_at": item.get("updated_at", 0) + 1,
                })
                student = next(
                    (row for row in students if row.get("student_id") == item.get("student_id")),
                    None,
                )
                was_active = previous_status in {"open", "in_progress"}
                is_active = item.get("status") in {"open", "in_progress"}
                if student is not None and was_active != is_active:
                    current_count = int(student.get("open_attention_count") or 0)
                    student["open_attention_count"] = max(
                        0, current_count + (1 if is_active else -1)
                    )
                    if student["open_attention_count"] == 0:
                        student["highest_attention_priority"] = None
                    else:
                        active = [
                            row for row in attention_items
                            if row.get("student_id") == item.get("student_id")
                            and row.get("status") in {"open", "in_progress"}
                        ]
                        if len(active) == student["open_attention_count"]:
                            student["highest_attention_priority"] = (
                                "high" if any(row.get("priority") == "high" for row in active)
                                else "medium"
                            )
                if attention_patch_hook is not None:
                    attention_patch_hook(dict(item))
                route.fulfill(json=item)
        elif path == "/api/mentor/messages/status":
            payload = json.loads(route.request.post_data or "{}")
            client_request_ids = list(payload.get("client_request_ids") or [])
            mentor_status_requests.append(client_request_ids)
            route.fulfill(json={
                "items": [
                    dict(mentor_message_statuses[client_request_id])
                    for client_request_id in client_request_ids
                    if client_request_id in mentor_message_statuses
                ],
            })
        elif path == "/api/mentor/message":
            payload = json.loads(route.request.post_data or "{}")
            mentor_messages.append(payload)
            client_request_id = payload.get("client_request_id")
            existing = mentor_message_statuses.get(client_request_id)
            if existing is None:
                message_number = len(mentor_messages)
                existing = {
                    "client_request_id": client_request_id,
                    "message_id": f"message-{message_number}",
                    "id": message_number,
                    "student_id": payload.get("student_id"),
                    "delivered": False,
                }
                if client_request_id:
                    mentor_message_statuses[client_request_id] = existing
            route.fulfill(json={
                "message_id": existing["message_id"],
                "id": existing["id"],
                "delivered": existing["delivered"],
            })
        elif path == "/api/mentor/students":
            route.fulfill(json={"items": students})
        elif path.startswith("/api/mentor/students/") and path.endswith("/request-upload"):
            sid = unquote(path[len("/api/mentor/students/"):-len("/request-upload")])
            request_id = "req-" + sid
            upload_requests.append(path)
            route.fulfill(json={"request_id": request_id, "status": "pending",
                                "student_id": sid, "session_id": "",
                                "transfer_status": "pending",
                                "analysis_status": "not_requested",
                                "transfer_error": "", "analysis_error": "",
                                "result": None})
        elif path.startswith("/api/mentor/upload-requests/") and path.endswith("/retry-analysis"):
            request_id = unquote(path[len("/api/mentor/upload-requests/"):-len("/retry-analysis")])
            upload_requests.append(path)
            payload = upload_retry_statuses.get(request_id, {
                "request_id": request_id, "student_id": "s1",
                "transfer_status": "stored", "analysis_status": "pending",
                "transfer_error": "", "analysis_error": "", "result": None,
            })
            route.fulfill(status=202, json=payload)
        elif path.startswith("/api/mentor/upload-requests/"):
            request_id = unquote(path[len("/api/mentor/upload-requests/"):])
            sequence = upload_statuses.get(request_id) or []
            if not sequence:
                route.fulfill(status=404, json={"detail": "not found"})
            else:
                payload = sequence.pop(0) if len(sequence) > 1 else sequence[0]
                route.fulfill(json=payload)
        elif path.startswith("/api/mentor/students/") and path.endswith("/sessions"):
            sid = unquote(path[len("/api/mentor/students/"):-len("/sessions")])
            route.fulfill(json={"items": sessions_by_student.get(sid, [])})
        elif path.startswith("/api/mentor/prompts/") and path.endswith("/reply"):
            pid = unquote(path[len("/api/mentor/prompts/"):-len("/reply")])
            if reply_status != 200:
                route.fulfill(status=reply_status, json={"detail": "boom"})
            else:
                route.fulfill(json={"reply": reply_by_prompt.get(pid, "")})
        elif path.startswith("/api/mentor/replies/") and path.endswith("/text"):
            ref = unquote(path[len("/api/mentor/replies/"):-len("/text")])
            if reply_status != 200:
                route.fulfill(status=reply_status, json={"detail": "boom"})
            else:
                route.fulfill(json={"reply": reply_by_ref.get(ref, reply_by_prompt.get(ref.removeprefix("prompt:"), ""))})
        elif path.startswith("/api/mentor/sessions/") and path.endswith("/timeline"):
            sess = unquote(path[len("/api/mentor/sessions/"):-len("/timeline")])
            route.fulfill(json={"items": timeline_by_session.get(sess, [])})
        elif path.startswith("/api/mentor/sessions/") and path.endswith("/transcript"):
            sess = unquote(path[len("/api/mentor/sessions/"):-len("/transcript")])
            if transcript_status != 200:
                route.fulfill(status=transcript_status, json={"detail": "boom"})
            else:
                payload = transcript_by_session.get(
                    sess, {"content": "", "created_at": 0}
                )
                route.fulfill(json=payload)
        else:
            route.fulfill(status=404, json={"detail": "not found"})

    page.route("**/api/mentor/**", handler)


def install_ws_route(page):
    """拦截 /ws/mentor：mock 一个保持打开的连接，避免真连后端 / 重连噪声。"""
    page.route_web_socket("**/ws/mentor", lambda ws: None)


def open_console(page, static_server, **routes):
    install_ws_route(page)
    install_api_routes(page, **routes)
    page.goto(static_server + "/index.html")
    return page


def bg(locator):
    """元素 computed 背景色（rgb 字符串）。"""
    return locator.evaluate("el => getComputedStyle(el).backgroundColor")


# ─────────────────────────────────────────────────────────────
# FE-1：学员列表渲染（display_name + 状态点 class）
# 负控：若 app.js 不渲染 display_name（例如渲染 student_id），文本断言必红；
#       若 severity→class 映射改错（error 不再 red），class 断言必红。
# ─────────────────────────────────────────────────────────────
def test_fe1_students_render(page, static_server):
    students = [
        {"student_id": "s1", "display_name": "王佳梁 Michael",
         "last_severity": "info", "session_count": 12, "analysis_count": 48},
        {"student_id": "s2", "display_name": "张三",
         "last_severity": "error", "session_count": 5,
         "analysis_count": 2, "alert_count": 3},
    ]
    open_console(page, static_server, students=students)

    items = page.locator(".student-item")
    expect(items).to_have_count(2)

    # 精确文本：渲染的是 display_name 而非 student_id
    expect(items.nth(0).locator(".name")).to_have_text("王佳梁 Michael")
    expect(items.nth(1).locator(".name")).to_have_text("张三")

    # 状态点 class：info→green，error→red
    assert "status-green" in items.nth(0).locator(".status-dot").get_attribute("class")
    assert "status-red" in items.nth(1).locator(".status-dot").get_attribute("class")

    # meta 内容（分析计数）真实渲染
    expect(items.nth(0).locator(".meta")).to_contain_text("48 分析")


# ─────────────────────────────────────────────────────────────
# FE-2（B3 回归）：点一个对话，另一个对话的状态圆点 class 不被刷绿、分析计数不清零。
# 若把 app.js 改回"从 DOM 反读重建"（selectSession 重算 severity），
# 未选中会话的 red 点会被刷成 green / 计数丢失 → 本用例必红。
# ─────────────────────────────────────────────────────────────
def test_fe2_b3_no_pseudo_status_reset(page, static_server):
    students = [
        {"student_id": "s1", "display_name": "王佳梁",
         "last_severity": "info", "session_count": 2, "analysis_count": 13},
    ]
    sessions = [
        {"session_id": "sess1", "session_title": "端口适配器重构",
         "last_severity": "info", "analysis_count": 8, "alert_count": 1},
        {"session_id": "sess2", "session_title": "浮标跨 Space 显示",
         "last_severity": "error", "analysis_count": 5, "alert_count": 2},
    ]
    timeline = {"sess1": [
        {"type": "prompt", "content": "删端口影响？", "created_at": 1719900000},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
    )

    page.locator(".student-item").first.click()
    sess_items = page.locator(".session-item")
    expect(sess_items).to_have_count(2)

    other = sess_items.nth(1)  # sess2，故意 error（红）
    # 点击前基线
    assert "status-red" in other.locator(".status-dot").get_attribute("class")
    expect(other.locator(".meta")).to_have_text("5 分析 · 2 告警")

    # 点第一个对话
    sess_items.nth(0).click()
    expect(sess_items.nth(0)).to_have_class(__import__("re").compile(r"\bselected\b"))
    # 时间线确有渲染（点击生效）
    expect(page.locator(".timeline .tl-row")).to_have_count(1)

    # 关键回归断言：另一个对话的状态点仍是 red、计数未清零
    assert "status-red" in other.locator(".status-dot").get_attribute("class"), \
        "B3 回归：未选中会话的状态圆点被错误刷新（应保持 error/red）"
    assert "status-green" not in other.locator(".status-dot").get_attribute("class")
    expect(other.locator(".meta")).to_have_text("5 分析 · 2 告警")


# ─────────────────────────────────────────────────────────────
# FE-5（XSS 回归）：学员可控文本（display_name / session_title）作为纯文本渲染，
# onerror 不执行（window.__xss 未置），且原文以 textContent 命中 DOM。
# 若 app.js 用 innerHTML 拼学员内容 → <img> 注入、onerror 触发 → 本用例必红。
# ─────────────────────────────────────────────────────────────
def test_fe5_xss_textcontent(page, static_server):
    students = [
        {"student_id": "s1", "display_name": XSS_PAYLOAD,
         "last_severity": "info", "session_count": 1, "analysis_count": 0},
    ]
    sessions = [
        {"session_id": "sess1", "session_title": XSS_PAYLOAD,
         "last_severity": "info", "analysis_count": 0, "alert_count": 0},
    ]
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
    )
    expect(page.locator(".student-item")).to_have_count(1)

    # 学员名：纯文本命中，未注入 img
    expect(page.locator(".student-item .name")).to_have_text(XSS_PAYLOAD)
    assert page.locator(".student-item img").count() == 0

    # 展开会话列表，检查 session_title 同样安全
    page.locator(".student-item").first.click()
    expect(page.locator(".session-item .name")).to_have_text(XSS_PAYLOAD)
    assert page.locator(".session-item img").count() == 0

    # 给潜在 onerror 一点执行窗口，然后断言脚本从未运行
    page.wait_for_timeout(150)
    assert page.evaluate("window.__xss") in (None, False), "XSS：onerror 被执行了！"
    assert page._console_errors == [], f"页面出现脚本错误: {page._console_errors}"


# ─────────────────────────────────────────────────────────────
# 四类型 + 命名：prompt/ai_summary/analysis/mentor_message 各自徽章与卡片 class；
# 文案为"AI 回复摘要"（非旧"AI 摘要"）。
# ─────────────────────────────────────────────────────────────
def test_four_types_and_naming(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 3}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "warn", "analysis_count": 3, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "prompt", "content": "帮我删端口", "created_at": 1719900000},
        {"type": "ai_summary", "content": "说明删除影响", "created_at": 1719900060},
        {"type": "analysis", "content": "卡在连锁影响", "severity": "error",
         "suggestion": "先跑测试", "is_technical": True, "topic": "重构",
         "created_at": 1719900120},
        {"type": "mentor_message", "content": "别急着删", "created_at": 1719900180},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()
    expect(page.locator(".timeline .tl-row")).to_have_count(4)

    # 四类徽章：class + 文案
    expect(page.locator(".tl-badge.badge-q")).to_have_text("问")
    expect(page.locator(".tl-badge.badge-ai")).to_have_text("AI")
    expect(page.locator(".tl-badge.badge-an")).to_have_text("诊")
    expect(page.locator(".tl-badge.badge-me")).to_have_text("师")

    # 四类卡片 class 均出现
    for cls in ("card-q", "card-ai", "card-an", "card-me"):
        assert page.locator(f".tl-card.{cls}").count() == 1, f"缺卡片 {cls}"

    # 命名："AI 回复摘要"（新），且不出现旧"AI 摘要"
    ai_top = page.locator(".card-ai .tl-top").inner_text()
    assert "AI 回复摘要" in ai_top, f"AI 卡顶部文案未更新: {ai_top!r}"
    assert "AI 摘要" not in ai_top, "仍出现旧文案 'AI 摘要'"

    # analysis 诊断卡带建议分区
    expect(page.locator(".card-an .tl-sug")).to_contain_text("先跑测试")


# ─────────────────────────────────────────────────────────────
# V-1（视觉）：四类型徽章 + 状态点，断言精确 computed rgb。
# 负控：把任一令牌改错一档（如 badge-an 从 #EF9F27 改成别的橙），
#       对应 rgb 断言立即变红——断言的是具体 rgb，不是"存在性"。
# ─────────────────────────────────────────────────────────────
def test_v1_visual_color_tokens(page, static_server):
    students = [
        {"student_id": "s1", "display_name": "红", "last_severity": "error",
         "session_count": 1, "analysis_count": 0},
        {"student_id": "s2", "display_name": "黄", "last_severity": "warn",
         "session_count": 1, "analysis_count": 0},
        {"student_id": "s3", "display_name": "绿", "last_severity": "info",
         "session_count": 1, "analysis_count": 0},
    ]
    sessions = [{"session_id": "sess1", "session_title": "s",
                 "last_severity": "info", "analysis_count": 4, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "prompt", "content": "q", "created_at": 1},
        {"type": "ai_summary", "content": "a", "created_at": 2},
        {"type": "analysis", "content": "d", "severity": "error", "created_at": 3},
        {"type": "mentor_message", "content": "m", "created_at": 4},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
    )
    dots = page.locator(".student-item .status-dot")
    expect(dots).to_have_count(3)

    # 三色状态点：red / yellow / green（精确 rgb）
    assert bg(dots.nth(0)) == HEX_TO_RGB["#ef4444"], "状态点 red 令牌不符"
    assert bg(dots.nth(1)) == HEX_TO_RGB["#f59e0b"], "状态点 yellow 令牌不符"
    assert bg(dots.nth(2)) == HEX_TO_RGB["#22c55e"], "状态点 green 令牌不符"

    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()
    expect(page.locator(".timeline .tl-row")).to_have_count(4)

    # 四类型徽章着色（精确 rgb）
    assert bg(page.locator(".badge-q")) == HEX_TO_RGB["#378ADD"], "prompt 蓝令牌不符"
    assert bg(page.locator(".badge-ai")) == HEX_TO_RGB["#534AB7"], "ai_summary 紫令牌不符"
    assert bg(page.locator(".badge-an")) == HEX_TO_RGB["#EF9F27"], "analysis 橙令牌不符"
    assert bg(page.locator(".badge-me")) == HEX_TO_RGB["#DB2777"], "mentor 粉令牌不符"


# 会话级原文入口的稳定定位符（时间线顶部单一入口）
TRANSCRIPT_ENTRY = "#transcript-entry"
# 用文案定位「入口」本身：无论谁实现成每条一个，都会命中多个 → 计数即负控
TRANSCRIPT_SUMMARY_BY_TEXT = "summary:has-text('查看完整对话原文')"


# ─────────────────────────────────────────────────────────────
# 单一入口 + 懒加载 + XSS：
#   时间线顶部只有 1 个「查看完整对话原文」入口（会话级，而非每条 ai_summary 一个）；
#   AI 摘要卡本身不再内嵌任何展开链接；
#   点该入口 → lazy fetch transcript → 正文经 textContent 渲染，XSS 不执行。
#
# 负控①：本用例喂了 **3 条 ai_summary**。若把入口做回"每条一个"，
#        `TRANSCRIPT_SUMMARY_BY_TEXT` 计数 = 3 ≠ 1 → to_have_count(1) 必红。
# 负控②：`.card-ai details` 断言为 0；若逐条内嵌 details 展开，此断言必红。
# ─────────────────────────────────────────────────────────────
def test_transcript_single_entry_lazy_expand_and_xss(page, static_server):
    full_text = "学员：帮我删端口\nAI：删除前需确认三点…" + XSS_PAYLOAD
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 3}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 3, "alert_count": 0}]
    # 故意放 3 条 AI 回复摘要：验证入口不随条目数增多而重复
    timeline = {"sess1": [
        {"type": "prompt", "content": "帮我删端口", "created_at": 90},
        {"type": "ai_summary", "content": "第一次回复摘要", "created_at": 100},
        {"type": "ai_summary", "content": "第二次回复摘要", "created_at": 110},
        {"type": "ai_summary", "content": "第三次回复摘要", "created_at": 120},
    ]}
    transcript = {"sess1": {"content": full_text, "created_at": 120}}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
        transcript_by_session=transcript,
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()

    # 3 条 AI 摘要都在，但入口只有 1 个
    expect(page.locator(".card-ai")).to_have_count(3)
    expect(page.locator(TRANSCRIPT_SUMMARY_BY_TEXT)).to_have_count(1)  # 关键：单一入口
    expect(page.locator(TRANSCRIPT_ENTRY)).to_have_count(1)
    expect(page.locator(TRANSCRIPT_ENTRY)).to_be_visible()             # 非空时间线 → 显示

    # AI 摘要卡本身不再带任何内嵌展开（旧的逐条 details 已移除）
    assert page.locator(".card-ai details").count() == 0
    assert page.locator(".card-ai .tl-fulltext").count() == 0

    body = page.locator("#transcript-body")
    # 展开前：正文尚未拉取
    assert full_text not in body.inner_text()

    # 点单一入口 → 触发 lazy fetch
    page.locator(TRANSCRIPT_SUMMARY_BY_TEXT).click()
    expect(body).to_contain_text("删除前需确认三点")  # 自动等待 fetch 完成

    # 完整对话原文经 textContent 命中；XSS payload 未执行
    assert XSS_PAYLOAD in body.inner_text()
    assert page.locator("#transcript-body img").count() == 0
    page.wait_for_timeout(100)
    assert page.evaluate("window.__xss") in (None, False)


# ─────────────────────────────────────────────────────────────
# 时间线为空 → 不显示原文入口。
# 负控：若无条件渲染入口，`to_be_hidden` 必红。
# ─────────────────────────────────────────────────────────────
def test_transcript_entry_hidden_when_timeline_empty(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 0}]
    sessions = [{"session_id": "sess1", "session_title": "空会话",
                 "last_severity": "info", "analysis_count": 0, "alert_count": 0}]
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session={"sess1": []},  # 空时间线
    )
    entry = page.locator(TRANSCRIPT_ENTRY)

    # 未选会话时（初始空态）入口即隐藏
    expect(entry).to_be_hidden()

    # 选一个空时间线的会话，入口仍隐藏
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()
    expect(page.locator(".timeline-empty")).to_be_visible()
    expect(entry).to_be_hidden()


# ─────────────────────────────────────────────────────────────
# 接线回归（失败态）：transcript 端点 500 → 展开区显示"加载失败"。
# ─────────────────────────────────────────────────────────────
def test_transcript_entry_failure(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 1}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 1, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "ai_summary", "content": "删除影响摘要", "created_at": 100},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
        transcript_status=500,
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()
    page.locator(TRANSCRIPT_SUMMARY_BY_TEXT).click()
    expect(page.locator("#transcript-body")).to_have_text("加载失败")


# ─────────────────────────────────────────────────────────────
# 真实数据形态：会话标题为空 → 列表显示"未命名对话"，绝不回退显示原始 session_id。
# 覆盖两种真实空标题：uuid 型会话与 hook-test 型会话；title 为纯空白也算空（trim）。
# 负控：若把 sessionTitleOf 改回 `|| s.session_id`，session_id 文本会出现 → 本用例必红。
# ─────────────────────────────────────────────────────────────
def test_session_empty_title_shows_placeholder(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 2, "analysis_count": 0}]
    # 真实历史会话形态：session_title 为空，session_id 是原始 uuid / hook 标识
    sessions = [
        {"session_id": "44cc4b2e-1d3e-4f5a-9b0c-1234567890ab", "session_title": "",
         "last_severity": "info", "analysis_count": 0, "alert_count": 0},
        {"session_id": "hook-test-3", "session_title": "   ",  # 纯空白也应视为空
         "last_severity": "info", "analysis_count": 0, "alert_count": 0},
    ]
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
    )
    page.locator(".student-item").first.click()
    names = page.locator(".session-item .name")
    expect(names).to_have_count(2)

    # 两条都显示占位标题，而不是原始 session_id
    expect(names.nth(0)).to_have_text("未命名对话")
    expect(names.nth(1)).to_have_text("未命名对话")

    # 负控：整份对话列表里绝不出现原始 session_id 文本
    list_text = page.locator(".session-list").inner_text()
    assert "44cc4b2e" not in list_text, "空标题回退暴露了原始 session_id(uuid)"
    assert "hook-test-3" not in list_text, "空标题回退暴露了原始 session_id(hook)"


# ─────────────────────────────────────────────────────────────
# 真实数据形态：历史会话 /transcript 返回 404 → 展开显示友好文案
# "（此会话暂无完整原文，可能是历史会话）"，而非"加载失败"。
# 负控：若把 404 也当作错误处理（沿用旧的 `if (!resp.ok) throw`），
#       展开会显示"加载失败" → 本用例必红。
# ─────────────────────────────────────────────────────────────
def test_transcript_404_shows_friendly_hint(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 1}]
    sessions = [{"session_id": "sess1", "session_title": "历史会话",
                 "last_severity": "info", "analysis_count": 1, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "ai_summary", "content": "删除影响摘要", "created_at": 100},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
        transcript_status=404,
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()
    page.locator(TRANSCRIPT_SUMMARY_BY_TEXT).click()

    body = page.locator("#transcript-body")
    expect(body).to_contain_text("暂无完整原文")  # 自动等待 fetch(404) 完成
    # 负控：404 属于"没有"而非"出错"，不得出现失败文案
    assert "加载失败" not in body.inner_text(), "404 被误当作加载失败"


# ─────────────────────────────────────────────────────────────
# V-新配色：三类卡片背景 computed rgb —— 提问=浅黄 / AI回复=浅绿 / 诊断=浅蓝；
#          诊断整行左缩进 > 提问行（视觉区分）。
# 负控：任一卡片背景改错一档，对应 rgb 断言必红；去掉 .row-an 缩进，缩进断言必红。
# ─────────────────────────────────────────────────────────────
def test_v_new_card_backgrounds_and_indent(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 3}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 3, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "prompt", "content": "问", "created_at": 1},
        {"type": "ai_summary", "content": "摘要", "created_at": 2},
        {"type": "analysis", "content": "诊断", "severity": "error", "created_at": 3},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()
    expect(page.locator(".timeline .tl-row")).to_have_count(3)

    # 三类卡片背景 computed rgb（精确断言）
    assert bg(page.locator(".card-q")) == "rgb(254, 249, 195)", "学员提问应为浅黄 #fef9c3"
    assert bg(page.locator(".card-ai")) == "rgb(220, 252, 231)", "AI 回复应为浅绿 #dcfce7"
    assert bg(page.locator(".card-an")) == "rgb(239, 246, 255)", "学习诊断应为浅蓝 #eff6ff"

    # 诊断整行左缩进 > 提问行
    def margin_left(loc):
        return loc.evaluate(
            "el => parseFloat(getComputedStyle(el).marginLeft) || 0"
        )
    prompt_row = page.locator(".tl-row:has(.card-q)")
    an_row = page.locator(".tl-row:has(.card-an)")
    assert margin_left(an_row) > margin_left(prompt_row), \
        "学习诊断行应比学员提问行有更大左缩进"


# ═════════════════════════════════════════════════════════════
# 功能 B：对话列表按空间/任务分组（参照 WorkBuddy 侧边栏）
# 真实数据形态：后端 /sessions 现返回全部会话，每条含
#   group_type: "space"|"task"|""，space_name（space 时有值），analysis_count 等。
# ═════════════════════════════════════════════════════════════

# 混合真实形态：2 个不同 space_name 的空间会话 + 3 个任务 + 1 个 group_type=""，
# 其中若干 analysis_count=0（未分析）。空间计数=2，任务计数=3+1=4。
GROUPED_STUDENTS = [
    {"student_id": "s1", "display_name": "王佳梁",
     "last_severity": "error", "session_count": 6, "analysis_count": 10},
]
GROUPED_SESSIONS = [
    # 空间：workbuddy-copilot（1 条，已分析 error）
    {"session_id": "sp1", "session_title": "浮标跨 Space 显示",
     "group_type": "space", "space_name": "workbuddy-copilot",
     "last_severity": "error", "analysis_count": 4, "alert_count": 2},
    # 空间：camp-tools（1 条，已分析 info）
    {"session_id": "sp2", "session_title": "配置同步",
     "group_type": "space", "space_name": "camp-tools",
     "last_severity": "info", "analysis_count": 1, "alert_count": 0},
    # 任务：已分析 warn
    {"session_id": "tk1", "session_title": "修 bug A",
     "group_type": "task", "space_name": "",
     "last_severity": "warn", "analysis_count": 2, "alert_count": 0},
    # 任务：全空（analysis_count=0 且 message_count=0）→ 应灰显
    {"session_id": "tk2", "session_title": "写测试",
     "group_type": "task", "space_name": "",
     "last_severity": "info", "analysis_count": 0, "alert_count": 0,
     "message_count": 0},
    # 任务：已分析 info
    {"session_id": "tk3", "session_title": "重构 X",
     "group_type": "task", "space_name": "",
     "last_severity": "info", "analysis_count": 3, "alert_count": 1},
    # group_type="" → 并入任务；全空 + 空标题（灰显）
    {"session_id": "ot1", "session_title": "",
     "group_type": "", "space_name": "",
     "last_severity": "info", "analysis_count": 0, "alert_count": 0,
     "message_count": 0},
]


# ─────────────────────────────────────────────────────────────
# B-1：分组标题与计数 + 空间按 space_name 二级分组 + group_type="" 并入任务。
# 负控：若不分组（仍平铺一层），`.group-header` 计数=0 → 必红；
#       若空间未按 space_name 二级分组，`.space-subgroup` 计数≠2 → 必红；
#       若把 group_type="" 误归空间，任务组内 `.session-item` 计数≠4 → 必红。
# ─────────────────────────────────────────────────────────────
def test_b1_sessions_grouped_by_space_and_task(page, static_server):
    open_console(
        page, static_server,
        students=GROUPED_STUDENTS,
        sessions_by_student={"s1": GROUPED_SESSIONS},
    )
    page.locator(".student-item").first.click()

    # 两个顶层分组：空间 + 任务
    headers = page.locator(".group-header")
    expect(headers).to_have_count(2)

    space_group = page.locator(".session-group[data-group='space']")
    task_group = page.locator(".session-group[data-group='task']")
    expect(space_group).to_have_count(1)
    expect(task_group).to_have_count(1)

    # 标题文案 + 真实计数（空间=2 会话，任务=4 会话）
    expect(space_group.locator(".group-title")).to_have_text("空间")
    expect(space_group.locator(".group-count")).to_have_text("2")
    expect(task_group.locator(".group-title")).to_have_text("任务")
    expect(task_group.locator(".group-count")).to_have_text("4")

    # 空间按 space_name 二级分组：2 个子分组，标题为两个不同 space_name
    subgroups = space_group.locator(".space-subgroup")
    expect(subgroups).to_have_count(2)
    sub_titles = space_group.locator(".subgroup-title")
    expect(sub_titles).to_have_count(2)
    names = {sub_titles.nth(0).inner_text(), sub_titles.nth(1).inner_text()}
    assert names == {"workbuddy-copilot", "camp-tools"}, f"二级分组名不符: {names}"

    # group_type="" 归入任务：任务组内恰好 4 条（tk1/tk2/tk3/ot1）
    expect(task_group.locator(".session-item")).to_have_count(4)
    # 空间组内恰好 2 条
    expect(space_group.locator(".session-item")).to_have_count(2)
    # 全部 6 条
    expect(page.locator(".session-item")).to_have_count(6)


# ─────────────────────────────────────────────────────────────
# B-2：全空会话（analysis_count=0 且 message_count=0）灰显 + 中性灰点 + meta="未分析"，仍可点击。
# 负控：若不弱化全空会话，opacity=1 → 断言 <1 必红；
#       若无分析仍用三色点（无 .status-none）→ 断言必红。
# ─────────────────────────────────────────────────────────────
def test_b2_unanalyzed_sessions_grayed(page, static_server):
    open_console(
        page, static_server,
        students=GROUPED_STUDENTS,
        sessions_by_student={"s1": GROUPED_SESSIONS},
    )
    page.locator(".student-item").first.click()

    # 未分析会话共 2 条（tk2、ot1）
    unan = page.locator(".session-item.unanalyzed")
    expect(unan).to_have_count(2)

    # 灰显：computed opacity < 1
    op = unan.first.evaluate("el => getComputedStyle(el).opacity")
    assert float(op) < 1.0, f"未分析会话应弱化，opacity={op}"

    # meta 文案为“未分析”，且用中性灰点（.status-none），不用三色
    expect(unan.first.locator(".meta")).to_have_text("未分析")
    assert unan.first.locator(".status-dot.status-none").count() == 1
    assert unan.first.locator(".status-dot.status-green").count() == 0

    # 已分析会话不带 unanalyzed 且 meta 含“分析”
    sp1 = page.locator(".session-item[data-session-id='sp1']")
    assert "unanalyzed" not in (sp1.get_attribute("class") or "")
    expect(sp1.locator(".meta")).to_have_text("4 分析 · 2 告警")


# ─────────────────────────────────────────────────────────────
# B-3：点未分析会话（timeline 为空）不报错，右栏显示“此对话暂无分析记录”。
# 负控：若点击链路抛错（如 group_type 处理不当），pageerror 非空 → 必红。
# ─────────────────────────────────────────────────────────────
def test_b3_click_unanalyzed_session_no_error(page, static_server):
    open_console(
        page, static_server,
        students=GROUPED_STUDENTS,
        sessions_by_student={"s1": GROUPED_SESSIONS},
        timeline_by_session={},  # 所有会话时间线为空
    )
    page.locator(".student-item").first.click()

    unan = page.locator(".session-item.unanalyzed").first
    unan.click()
    expect(unan).to_have_class(__import__("re").compile(r"\bselected\b"))

    # 右栏空态提示（无分析记录），而非报错
    empty = page.locator(".timeline-empty")
    expect(empty).to_be_visible()
    expect(empty).to_have_text("此对话暂无分析记录")

    # 点击链路无脚本异常
    page.wait_for_timeout(100)
    assert page._console_errors == [], f"点未分析会话出现脚本错误: {page._console_errors}"


# ─────────────────────────────────────────────────────────────
# B-2b：灰显判据升级 —— 从"analysis_count==0"改为"既无内容也无分析"。
#   新上传流程会先落库对话内容(message_count>0)、LLM 诊断后台异步补，
#   故"有内容但未诊断"的会话应立刻点亮（不灰显），不等 LLM。
# 三种会话形态：
#   ① 有分析(analysis_count>0)            → 正常显示（非 unanalyzed），meta "X 分析 · Y 告警"
#   ② 有内容无分析(message_count>0,        → 不灰显（非 unanalyzed），中性灰点，
#      analysis_count=0)                     meta 含"对话"/"待诊断"
#   ③ 全空(analysis_count=0, message_count=0)→ 灰显(unanalyzed)，中性灰点，meta "未分析"
# 负控：若把判据改回"只看 analysis_count==0"，用例②(analysis_count=0)会被误灰
#       → "②非 unanalyzed / opacity==1"断言必红。
# ─────────────────────────────────────────────────────────────
def test_b2b_content_without_analysis_not_grayed(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 3, "analysis_count": 5}]
    sessions = [
        # ① 有分析
        {"session_id": "an1", "session_title": "已诊断",
         "group_type": "task", "space_name": "",
         "last_severity": "warn", "analysis_count": 5, "alert_count": 1,
         "message_count": 20},
        # ② 有内容无分析（新形态：内容已上传，诊断异步补）
        {"session_id": "ct1", "session_title": "已上传待诊断",
         "group_type": "task", "space_name": "",
         "last_severity": "info", "analysis_count": 0, "alert_count": 0,
         "message_count": 12},
        # ③ 全空
        {"session_id": "em1", "session_title": "空会话",
         "group_type": "task", "space_name": "",
         "last_severity": "info", "analysis_count": 0, "alert_count": 0,
         "message_count": 0},
    ]
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
    )
    page.locator(".student-item").first.click()

    an1 = page.locator(".session-item[data-session-id='an1']")
    ct1 = page.locator(".session-item[data-session-id='ct1']")
    em1 = page.locator(".session-item[data-session-id='em1']")

    # ① 有分析：非 unanalyzed，meta 显示分析计数
    assert "unanalyzed" not in (an1.get_attribute("class") or "")
    expect(an1.locator(".meta")).to_have_text("5 分析 · 1 告警")

    # ② 有内容无分析：关键——不灰显（非 unanalyzed），meta 含"对话"/"待诊断"
    assert "unanalyzed" not in (ct1.get_attribute("class") or ""), \
        "有内容(message_count>0)的会话不应灰显，即使还没诊断"
    op2 = ct1.evaluate("el => getComputedStyle(el).opacity")
    assert float(op2) == 1.0, f"有内容会话应正常显示 opacity=1，实际 {op2}"
    meta2 = ct1.locator(".meta").inner_text()
    assert ("对话" in meta2) or ("待诊断" in meta2), \
        f"有内容待诊断会话 meta 应含'对话'或'待诊断'，实际 {meta2!r}"
    # 中性灰点（不用三色告警语义）
    assert ct1.locator(".status-dot.status-none").count() == 1
    assert ct1.locator(".status-dot.status-green").count() == 0

    # ③ 全空：灰显(unanalyzed) + meta "未分析" + 中性灰点
    assert "unanalyzed" in (em1.get_attribute("class") or ""), \
        "既无内容也无分析的会话应灰显"
    op3 = em1.evaluate("el => getComputedStyle(el).opacity")
    assert float(op3) < 1.0, f"全空会话应弱化，opacity={op3}"
    expect(em1.locator(".meta")).to_have_text("未分析")
    assert em1.locator(".status-dot.status-none").count() == 1


# ─────────────────────────────────────────────────────────────
# B-4：折叠——点分组标题收起，其子项隐藏；再点展开恢复；另一分组不受影响。
# 负控：若未实现折叠（body 始终可见），点击后 to_be_hidden() 必红。
# ─────────────────────────────────────────────────────────────
def test_b4_group_collapse_hides_children(page, static_server):
    open_console(
        page, static_server,
        students=GROUPED_STUDENTS,
        sessions_by_student={"s1": GROUPED_SESSIONS},
    )
    page.locator(".student-item").first.click()

    space_group = page.locator(".session-group[data-group='space']")
    task_group = page.locator(".session-group[data-group='task']")
    space_item = space_group.locator(".session-item").first
    task_item = task_group.locator(".session-item").first

    # 初始展开：两组子项均可见
    expect(space_item).to_be_visible()
    expect(task_item).to_be_visible()

    # 点空间分组标题 → 空间子项隐藏，任务子项不受影响
    space_group.locator(".group-header").click()
    expect(space_item).to_be_hidden()
    expect(task_item).to_be_visible()

    # 再点一次 → 展开恢复
    space_group.locator(".group-header").click()
    expect(space_item).to_be_visible()


# ─────────────────────────────────────────────────────────────
# B-5（Point 1）：对话列表排序 + 分组折叠箭头放大。
#   同组内 / 子组内按 last_activity_at 倒序（最新在最上）；
#   空间各 space_name 子组之间按"各子组最新会话时间"倒序；空间大组恒在任务大组之前。
#   分组折叠箭头字号较旧值（10px）放大（≥14px）。
# 负控：若依赖后端顺序 / 保持插入顺序不排序，DOM 顺序断言必红；
#       若箭头未放大（仍 10px），字号断言必红。
# ─────────────────────────────────────────────────────────────
# 乱序放入，强制纯前端 sort（若保持插入顺序则断言必红）。
#   空间 alpha：a1(300) / a2(100) → 子组最新=300
#   空间 beta ：b1(500)           → 子组最新=500 → beta 排在 alpha 前
#   任务：tk_old(50) / tk_mid(400) / tk_new(900) → 倒序 tk_new→tk_mid→tk_old
SORTED_SESSIONS = [
    {"session_id": "a2", "session_title": "alpha 旧", "group_type": "space",
     "space_name": "alpha", "last_activity_at": 100,
     "analysis_count": 1, "alert_count": 0, "message_count": 5},
    {"session_id": "tk_mid", "session_title": "任务 中", "group_type": "task",
     "space_name": "", "last_activity_at": 400,
     "analysis_count": 1, "alert_count": 0, "message_count": 5},
    {"session_id": "b1", "session_title": "beta 唯一", "group_type": "space",
     "space_name": "beta", "last_activity_at": 500,
     "analysis_count": 1, "alert_count": 0, "message_count": 5},
    {"session_id": "tk_old", "session_title": "任务 旧", "group_type": "task",
     "space_name": "", "last_activity_at": 50,
     "analysis_count": 1, "alert_count": 0, "message_count": 5},
    {"session_id": "a1", "session_title": "alpha 新", "group_type": "space",
     "space_name": "alpha", "last_activity_at": 300,
     "analysis_count": 1, "alert_count": 0, "message_count": 5},
    {"session_id": "tk_new", "session_title": "任务 新", "group_type": "task",
     "space_name": "", "last_activity_at": 900,
     "analysis_count": 1, "alert_count": 0, "message_count": 5},
]


def test_b5_sessions_sorted_by_activity_and_caret_enlarged(page, static_server):
    open_console(
        page, static_server,
        students=GROUPED_STUDENTS,
        sessions_by_student={"s1": SORTED_SESSIONS},
    )
    page.locator(".student-item").first.click()

    # 大组顺序固定：空间在任务之前
    groups = page.locator(".session-group")
    expect(groups).to_have_count(2)
    assert groups.nth(0).get_attribute("data-group") == "space", "空间大组应在最上"
    assert groups.nth(1).get_attribute("data-group") == "task", "任务大组应在空间之后"

    space_group = page.locator(".session-group[data-group='space']")
    task_group = page.locator(".session-group[data-group='task']")

    # 空间子组之间：按各子组最新会话时间倒序 → beta(500) 在 alpha(300) 之前
    sub_titles = [t.inner_text() for t in space_group.locator(".subgroup-title").all()]
    assert sub_titles == ["beta", "alpha"], f"空间子组未按最新时间倒序: {sub_titles}"

    # 空间组内会话 DOM 顺序：b1(500) → a1(300) → a2(100)
    space_ids = [it.get_attribute("data-session-id")
                 for it in space_group.locator(".session-item").all()]
    assert space_ids == ["b1", "a1", "a2"], \
        f"空间组内/子组内未按 last_activity_at 倒序: {space_ids}"

    # 任务组内会话 DOM 顺序：tk_new(900) → tk_mid(400) → tk_old(50)
    task_ids = [it.get_attribute("data-session-id")
                for it in task_group.locator(".session-item").all()]
    assert task_ids == ["tk_new", "tk_mid", "tk_old"], \
        f"任务组内未按 last_activity_at 倒序: {task_ids}"

    # 分组折叠箭头放大：字号较旧值（10px）明显放大（≥14px）
    caret_fs = page.locator(".group-caret").first.evaluate(
        "el => parseFloat(getComputedStyle(el).fontSize)"
    )
    assert caret_fs >= 14, f"分组折叠箭头字号应放大到 ≥14px，实际 {caret_fs}px"


# ═════════════════════════════════════════════════════════════
# 功能 C：同步该学员全部对话按钮
#   选中学员 → 按钮可用 → 点击发 POST /request-upload + 显示反馈；未选中时禁用。
# ═════════════════════════════════════════════════════════════
def test_c_sync_button_requests_upload(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 2, "analysis_count": 3}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 3, "alert_count": 0}]
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
    )
    btn = page.locator("#sync-student")

    # 未选中学员：按钮禁用（负控：若无条件启用，to_be_disabled 必红）
    expect(btn).to_be_disabled()

    # 选中学员 → 按钮启用
    page.locator(".student-item").first.click()
    expect(btn).to_be_enabled()

    # 点击 → 发出 POST /api/mentor/students/s1/request-upload
    with page.expect_request(
        lambda r: r.url.endswith("/api/mentor/students/s1/request-upload")
        and r.method == "POST"
    ) as req_info:
        btn.click()
    req = req_info.value
    assert req.method == "POST", f"应为 POST，实际 {req.method}"

    # 反馈文案出现
    feedback = page.locator("#sync-feedback")
    expect(feedback).to_be_visible()
    expect(feedback).to_contain_text("已请求同步")


def test_upload_status_ws_then_poll_failure_and_retry_only_analysis(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 0, "analysis_count": 0}]
    xss_error = '<img src=x onerror="window.__xss=1">诊断超时'
    failed = {
        "request_id": "req-s1", "student_id": "s1", "session_id": "sess-1",
        "transfer_status": "stored", "analysis_status": "failed",
        "transfer_error": "", "analysis_error": xss_error,
        "result": {"synced": 1},
    }
    done = dict(failed, analysis_status="done", analysis_error="")
    upload_statuses = {"req-s1": [failed, done]}
    upload_requests = []
    ws_holder = {}

    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    install_api_routes(
        page,
        students=students,
        sessions_by_student={"s1": []},
        upload_statuses=upload_statuses,
        upload_retry_statuses={"req-s1": dict(failed, analysis_status="pending", analysis_error="")},
        upload_requests=upload_requests,
    )
    page.goto(static_server + "/index.html")
    page.locator(".student-item").first.click()
    page.locator("#sync-student").click()
    expect(page.locator("#sync-feedback")).to_contain_text("已请求同步")

    page.wait_for_function("() => window.WebSocket && document.querySelector('#ws-status').textContent === '已连接'")
    ws_holder["socket"].send(__import__("json").dumps({
        "type": "upload_request_status",
        "request_id": "req-s1",
        "student_id": "s1",
        "transfer_status": "running",
        "analysis_status": "not_requested",
        "transfer_error": "",
        "analysis_error": "",
        "result": None,
    }))
    expect(page.locator("#sync-feedback")).to_contain_text("正在上传")

    # 轮询读取数据库快照后进入真实失败态，服务端错误仅作为纯文本显示。
    expect(page.locator("#sync-feedback")).to_contain_text("诊断失败", timeout=4000)
    expect(page.locator("#sync-feedback")).to_contain_text("诊断超时")
    expect(page.locator("#retry-analysis")).to_be_visible()
    assert page.locator("#sync-feedback img").count() == 0
    assert page.evaluate("window.__xss") in (None, False)

    # 点击只请求 retry-analysis，不重新触发整批上传。
    page.locator("#retry-analysis").click()
    expect(page.locator("#sync-feedback")).to_contain_text("诊断完成", timeout=4000)
    assert upload_requests.count("/api/mentor/students/s1/request-upload") == 1
    assert upload_requests.count("/api/mentor/upload-requests/req-s1/retry-analysis") == 1


def test_old_poll_cannot_overwrite_new_student_upload_request(page, static_server):
    students = [
        {"student_id": "old", "display_name": "旧学员", "last_severity": "info"},
        {"student_id": "new", "display_name": "新学员", "last_severity": "info"},
    ]
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = String(input);
          if (url.includes('/api/mentor/upload-requests/req-old') &&
              !(options && String(options.method || 'GET').toUpperCase() === 'POST')) {
            window.__oldPollSignal = options && options.signal;
            return new Promise((resolve) => {
              window.__resolveOldPoll = () => resolve(new Response(JSON.stringify({
                request_id: 'req-old', student_id: 'old', transfer_status: 'failed',
                analysis_status: 'not_requested', transfer_error: 'STALE-ERROR',
                analysis_error: '', result: null
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    open_console(
        page,
        static_server,
        students=students,
        sessions_by_student={"old": [], "new": []},
        upload_statuses={"req-new": [{
            "request_id": "req-new", "student_id": "new",
            "transfer_status": "stored", "analysis_status": "not_requested",
            "transfer_error": "", "analysis_error": "", "result": {"synced": 2},
        }]},
    )

    page.locator(".student-item").nth(0).click()
    page.locator("#sync-student").click()
    page.wait_for_function("() => typeof window.__resolveOldPoll === 'function'", timeout=4000)
    page.locator(".student-item").nth(1).click()
    assert page.evaluate("window.__oldPollSignal && window.__oldPollSignal.aborted") is True
    page.locator("#sync-student").click()
    expect(page.locator("#sync-feedback")).to_contain_text("内容已保存", timeout=4000)

    page.evaluate("window.__resolveOldPoll()")
    page.wait_for_timeout(150)
    expect(page.locator("#sync-feedback")).to_contain_text("内容已保存")
    assert "STALE-ERROR" not in page.locator("#sync-feedback").inner_text()


def test_older_ws_snapshot_cannot_regress_polled_terminal_state(page, static_server):
    students = [{"student_id": "s1", "display_name": "学员", "last_severity": "info"}]
    ws_holder = {}
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.route_web_socket("**/ws/mentor", lambda ws: ws_holder.setdefault("socket", ws))
    install_api_routes(
        page,
        students=students,
        sessions_by_student={"s1": []},
        upload_statuses={"req-s1": [
            {
                "request_id": "req-s1", "student_id": "s1", "updated_at": 20,
                "transfer_status": "running", "analysis_status": "running",
                "transfer_error": "", "analysis_error": "", "result": None,
            },
            {
                "request_id": "req-s1", "student_id": "s1", "updated_at": 30,
                "transfer_status": "stored", "analysis_status": "done",
                "transfer_error": "", "analysis_error": "", "result": {"synced": 1},
            },
        ]},
    )
    page.goto(static_server + "/index.html")
    page.locator(".student-item").click()
    page.locator("#sync-student").click()
    expect(page.locator("#sync-feedback")).to_contain_text("正在上传", timeout=4000)

    ws_holder["socket"].send(__import__("json").dumps({
        "type": "upload_request_status", "request_id": "req-s1", "student_id": "s1",
        "updated_at": 10, "transfer_status": "running", "analysis_status": "running",
        "transfer_error": "", "analysis_error": "", "result": None,
    }))
    # 旧 WS 不得取消仍需继续的权威轮询。
    expect(page.locator("#sync-feedback")).to_contain_text("诊断完成", timeout=4000)
    poll_count = len([url for url in requests if "/upload-requests/req-s1" in url])
    ws_holder["socket"].send(__import__("json").dumps({
        "type": "upload_request_status", "request_id": "req-s1", "student_id": "s1",
        "updated_at": 10, "transfer_status": "running", "analysis_status": "running",
        "transfer_error": "", "analysis_error": "", "result": None,
    }))
    page.wait_for_timeout(1000)
    expect(page.locator("#sync-feedback")).to_contain_text("诊断完成")
    assert len([url for url in requests if "/upload-requests/req-s1" in url]) == poll_count


def test_a_b_a_slow_old_post_cannot_replace_new_a_request(page, static_server):
    students = [
        {"student_id": "a", "display_name": "A", "last_severity": "info"},
        {"student_id": "b", "display_name": "B", "last_severity": "info"},
    ]
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        let aPosts = 0;
        window.fetch = (input, options) => {
          const url = String(input);
          const method = String((options && options.method) || 'GET').toUpperCase();
          if (url.includes('/api/mentor/students/a/request-upload') && method === 'POST') {
            aPosts += 1;
            if (aPosts === 1) {
              return new Promise((resolve) => {
                window.__resolveOldAPost = () => resolve(new Response(JSON.stringify({
                  request_id: 'req-old-a', student_id: 'a', transfer_status: 'pending',
                  analysis_status: 'not_requested', transfer_error: '', analysis_error: '',
                  result: null, updated_at: 1
                }), {status: 200, headers: {'Content-Type': 'application/json'}}));
              });
            }
          }
          return realFetch(input, options);
        };
      })();
    """)
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"a": [], "b": []},
        upload_statuses={"req-a": [{
            "request_id": "req-a", "student_id": "a", "updated_at": 20,
            "transfer_status": "stored", "analysis_status": "done",
            "transfer_error": "", "analysis_error": "", "result": {"synced": 1},
        }]},
    )
    page.locator(".student-item").nth(0).click()
    page.locator("#sync-student").click()
    page.wait_for_function("() => typeof window.__resolveOldAPost === 'function'")
    page.locator(".student-item").nth(1).click()
    page.locator(".student-item").nth(0).click()
    page.locator("#sync-student").click()
    expect(page.locator("#sync-feedback")).to_contain_text("诊断完成", timeout=4000)

    page.evaluate("window.__resolveOldAPost()")
    page.wait_for_timeout(200)
    expect(page.locator("#sync-feedback")).to_contain_text("诊断完成")


# ═════════════════════════════════════════════════════════════
# 功能 D：每条 ai_summary "显示详情"热加载该次提问的完整回复（逐条独立）。
#   2 条 ai_summary，各带 reply_ref → 默认只显示摘要 + 各自"显示详情"按钮；
#   点第一条"显示详情"→ 热加载并展开第一条完整回复，第二条不受影响（仍收起、内容不同）。
#
# 负控①：若把展开做成显示同一段（如共享的整会话原文），
#         "第二条卡片不含第一条唯一标识 ALPHA" 断言必红。
# 负控②：若不做逐条展开（无 .ai-detail-toggle 或点击不改本条 state），
#         按钮计数 / 详情面板出现断言必红。
# ═════════════════════════════════════════════════════════════
AI_REPLY_1 = (
    "第一轮完整回复：\n"
    "步骤一：先跑现有测试确认基线。\n"
    "步骤二：定位端口依赖链。\n"
    "步骤三：逐个替换适配器。\n"
    "步骤四：回归验证。\n"
    "步骤五：更新文档。\n"
    "唯一结论标识 ALPHA"
)
AI_REPLY_2 = (
    "第二轮完整回复：\n"
    "方案甲：直接删除旧端口。\n"
    "方案乙：并存过渡期。\n"
    "方案丙：feature flag 切换。\n"
    "方案丁：灰度发布。\n"
    "方案戊：回滚预案。\n"
    "唯一结论标识 BETA"
)


def test_d_per_round_ai_reply_expand(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 2}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 2, "alert_count": 0}]
    # 两条 bulk ai_summary 通过 reply_ref 热加载各自完整回复。
    timeline = {"sess1": [
        {"type": "ai_summary", "content": "第一轮摘要", "reply_ref": "msg:101",
         "has_full_reply": True, "created_at": 100},
        {"type": "ai_summary", "content": "第二轮摘要", "reply_ref": "msg:102",
         "has_full_reply": True, "created_at": 110},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
        reply_by_ref={"msg:101": AI_REPLY_1, "msg:102": AI_REPLY_2},
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()

    cards = page.locator(".card-ai")
    expect(cards).to_have_count(2)
    card1 = cards.nth(0)
    card2 = cards.nth(1)
    expect(card1.locator(".ai-summary")).to_have_text("第一轮摘要")
    expect(card2.locator(".ai-summary")).to_have_text("第二轮摘要")

    # 默认：摘要直接显示，完整回复还没加载；每条有独立详情按钮。
    toggle1 = card1.locator(".ai-detail-toggle")
    toggle2 = card2.locator(".ai-detail-toggle")
    expect(toggle1).to_be_visible()
    expect(toggle2).to_be_visible()
    expect(toggle1).to_contain_text("显示详情")
    expect(toggle2).to_contain_text("显示详情")
    expect(card1.locator(".ai-detail")).to_have_count(0)
    expect(card2.locator(".ai-detail")).to_have_count(0)

    # 点第一条展开 → 只影响第一条
    toggle1.click()

    # 第一条：热加载后显示本条完整回复，按钮变"隐藏"，卡内含本条唯一标识 ALPHA
    expect(toggle1).to_contain_text("隐藏")
    expect(card1.locator(".ai-detail")).to_contain_text("ALPHA")
    assert "ALPHA" in card1.inner_text(), "第一条展开后应显示本条自己的全文（ALPHA）"

    # 第二条：不受影响，仍未展开、按钮仍"显示详情"
    expect(card2.locator(".ai-detail")).to_have_count(0)
    expect(toggle2).to_contain_text("显示详情")

    # 负控：每轮展开的是各自内容，不是同一段
    #   第二条卡片不得出现第一条的唯一标识（若共用整会话原文，此断言必红）
    assert "ALPHA" not in card2.inner_text(), \
        "第二条不应含第一条唯一标识——每轮必须展开各自的全文，而非同一段"
    assert "BETA" not in card1.inner_text(), \
        "第一条不应含第二条唯一标识——每轮内容互相独立"

    # 收起第一条 -> 详情面板消失，按钮回到显示详情
    toggle1.click()
    expect(card1.locator(".ai-detail")).to_have_count(0)
    expect(toggle1).to_contain_text("显示详情")


# ═════════════════════════════════════════════════════════════
# 功能 D2（Point 2 核心）：AI 回复摘要「显示详情」热加载 + 缓存 + XSS。
#   点"显示详情" → 恰好一次 GET /replies/{reply_ref}/text → 卡下方同款面板展开完整原文
#                 （textContent 防 XSS）→ 按钮变"隐藏"；
#   点"隐藏" → 面板收起；
#   再点"显示详情" → 命中缓存不重新请求（reply 请求数恒=1），直接展开。
#
# 缓存负控：若不缓存（每次展开都请求），"reply 请求数==1"断言必红。
# XSS 负控：若用 innerHTML 渲染完整原文，注入 <img> 触发 onerror → window.__xss 被置 → 必红。
# ═════════════════════════════════════════════════════════════
AI_FULL_REPLY_XSS = (
    "完整回复原文：\n"
    "第一步：先跑现有测试确认基线。\n"
    "第二步：定位端口依赖链。\n"
    "唯一标识 GAMMA " + XSS_PAYLOAD
)


def test_d2_ai_reply_hotload_cache_and_xss(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 1}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 1, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "ai_summary", "content": "该提问的 AI 回复摘要（50-300字，平均约100字，正常显示）",
         "prompt_id": 77, "has_full_reply": True, "created_at": 100},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
        reply_by_prompt={"77": AI_FULL_REPLY_XSS},
    )

    # 统计对 /replies/{reply_ref}/text 的请求次数（缓存负控用）
    reply_reqs = []
    page.on("request",
            lambda r: reply_reqs.append(r.url) if r.url.endswith("/text") else None)

    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()

    card = page.locator(".card-ai")
    expect(card).to_have_count(1)
    toggle = card.locator(".ai-detail-toggle")

    # 默认：只显示摘要 + "显示详情"按钮，无完整原文、无展开面板
    expect(card.locator(".ai-summary")).to_have_text("该提问的 AI 回复摘要（50-300字，平均约100字，正常显示）")
    expect(toggle).to_contain_text("显示详情")
    expect(card.locator(".ai-detail")).to_have_count(0)
    assert "GAMMA" not in card.inner_text()

    # 点"显示详情" → 恰好一次请求 + 展开原文 + 按钮变"隐藏"
    with page.expect_request(lambda r: r.url.endswith("/api/mentor/replies/prompt%3A77/text")):
        toggle.click()
    panel = card.locator(".ai-detail")
    expect(panel).to_contain_text("第一步")     # 自动等待 fetch 完成
    expect(toggle).to_contain_text("隐藏")
    assert "GAMMA" in panel.inner_text()
    assert len(reply_reqs) == 1, f"首次展开应恰好一次请求，实际 {len(reply_reqs)}"

    # 完整原文经 textContent 命中；XSS 未执行（无注入 img、window.__xss 未置）
    assert card.locator(".ai-detail img").count() == 0
    page.wait_for_timeout(100)
    assert page.evaluate("window.__xss") in (None, False), "XSS：onerror 被执行了！"

    # 点"隐藏" → 面板收起、按钮回到"显示详情"
    toggle.click()
    expect(card.locator(".ai-detail")).to_have_count(0)
    expect(toggle).to_contain_text("显示详情")

    # 再点"显示详情" → 命中缓存，不重新请求，直接展开
    toggle.click()
    expect(card.locator(".ai-detail")).to_contain_text("第一步")
    page.wait_for_timeout(150)  # 给潜在的二次请求留出窗口
    assert len(reply_reqs) == 1, \
        f"缓存负控：隐藏后再展开不应重新请求（应恒为 1 次），实际 {len(reply_reqs)}"
    assert page._console_errors == [], f"页面出现脚本错误: {page._console_errors}"


# ─────────────────────────────────────────────────────────────
# 功能 D3：空摘要占位 + 详情加载失败 + 无 reply_ref 不给"显示详情"。
#   ① content 为空 → 正文显示"（未生成摘要）"；有 reply_ref → 有按钮，
#      点后 reply 端点 500 → 面板显示"加载失败"。
#   ② 无 reply_ref 的摘要 → 不显示"显示详情"按钮（无从拉取完整回复）。
# 负控：若空摘要不占位（渲染空白）→ "（未生成摘要）"断言必红；
#       若失败态不提示 → "加载失败"断言必红；
#       若无 reply_ref 也给按钮 → 按钮计数==0 断言必红。
# ─────────────────────────────────────────────────────────────
def test_d3_ai_summary_placeholder_and_failure(page, static_server):
    students = [{"student_id": "s1", "display_name": "王佳梁",
                 "last_severity": "info", "session_count": 1, "analysis_count": 2}]
    sessions = [{"session_id": "sess1", "session_title": "重构",
                 "last_severity": "info", "analysis_count": 2, "alert_count": 0}]
    timeline = {"sess1": [
        {"type": "ai_summary", "content": "", "reply_ref": "msg:88",
         "has_full_reply": True, "created_at": 100},
        {"type": "ai_summary", "content": "只有摘要，没有 reply_ref",
         "created_at": 110},
    ]}
    open_console(
        page, static_server,
        students=students,
        sessions_by_student={"s1": sessions},
        timeline_by_session=timeline,
        reply_status=500,  # 详情热加载失败
    )
    page.locator(".student-item").first.click()
    page.locator(".session-item").first.click()

    cards = page.locator(".card-ai")
    expect(cards).to_have_count(2)
    card1, card2 = cards.nth(0), cards.nth(1)

    # ① 空 content → 占位"（未生成摘要）"
    expect(card1.locator(".ai-summary")).to_have_text("（未生成摘要）")
    # ① 有 reply_ref → 有"显示详情"按钮；点击 → reply 500 → 面板显示"加载失败"
    toggle1 = card1.locator(".ai-detail-toggle")
    expect(toggle1).to_be_visible()
    toggle1.click()
    expect(card1.locator(".ai-detail")).to_have_text("加载失败")

    # ② 无 reply_ref → 不显示"显示详情"按钮（无从拉取完整回复）
    expect(card2.locator(".ai-summary")).to_have_text("只有摘要，没有 reply_ref")
    assert card2.locator(".ai-detail-toggle").count() == 0, \
        "无 reply_ref 的摘要不应出现'显示详情'按钮"


# ───────────────────────────────────────────────────────────
# Task 6: 导师干预雷达。关注队列只做导师决策辅助，建议绝不自动发送。
# ───────────────────────────────────────────────────────────
def _attention_item(
    item_id,
    *,
    student_id="s1",
    session_id="sess1",
    priority="high",
    category="learning",
    status="open",
    reason="卡在明确的技术边界",
    suggested_action="请学员先复现最小失败用例",
    evidence=None,
    confidence=0.9,
    created_at=10,
):
    return {
        "id": item_id,
        "source_type": "analysis" if category == "learning" else "system",
        "source_id": str(item_id),
        "category": category,
        "student_id": student_id,
        "session_id": session_id,
        "priority": priority,
        "reason_code": "analysis_error" if category == "learning" else "system_bulk_analysis_failed",
        "reason": reason,
        "evidence": evidence if evidence is not None else ["连续两次在同一步失败"],
        "suggested_action": suggested_action,
        "confidence": confidence,
        "status": status,
        "handled_by": "",
        "resolution_note": "",
        "handled_at": None,
        "created_at": created_at,
        "updated_at": created_at,
    }


def test_attention_queue_sorts_filters_and_renders_user_text_safely(
    page,
    static_server,
):
    malicious = XSS_PAYLOAD + "诊断"
    students = [
        {"student_id": "s1", "display_name": "学员甲", "last_severity": "info",
         "session_count": 1, "analysis_count": 1, "open_attention_count": 2,
         "highest_attention_priority": "high", "last_attention_at": 20},
        {"student_id": "s2", "display_name": "学员乙", "last_severity": "warn",
         "session_count": 1, "analysis_count": 1, "open_attention_count": 1,
         "highest_attention_priority": "medium", "last_attention_at": 30},
    ]
    items = [
        _attention_item(3, student_id="s2", session_id="sess2", priority="medium",
                        category="system", created_at=1),
        _attention_item(2, reason=malicious, evidence=[malicious],
                        suggested_action=malicious, created_at=20),
        _attention_item(1, created_at=10),
        _attention_item(4, status="resolved", created_at=0),
    ]
    open_console(page, static_server, students=students, attention_items=items)

    cards = page.locator(".attention-card")
    expect(cards).to_have_count(3)  # 默认 active，不展示 resolved
    assert cards.evaluate_all(
        "nodes => nodes.map(node => Number(node.dataset.attentionId))"
    ) == [1, 2, 3]
    expect(cards.nth(0).locator(".attention-student-name")).to_have_text("学员甲")
    expect(cards.nth(0).locator(".attention-priority")).to_have_text("高")
    expect(cards.nth(0).locator(".attention-evidence")).to_contain_text("连续两次在同一步失败")
    expect(cards.nth(0).locator(".attention-confidence")).to_have_text("置信度 90%")
    expect(cards.nth(0).locator(".attention-action")).to_contain_text(
        "请学员先复现最小失败用例"
    )
    expect(cards.nth(2).locator(".attention-category")).to_have_text("系统异常")
    expect(cards.nth(1).locator(".attention-reason")).to_have_text(malicious)
    assert cards.nth(1).locator("img").count() == 0
    assert page.evaluate("window.__xss") in (None, False)

    page.locator("#attention-priority-filter").select_option("medium")
    expect(cards).to_have_count(1)
    expect(cards.nth(0)).to_have_attribute("data-attention-id", "3")

    page.locator("#attention-priority-filter").select_option("")
    page.locator("#attention-student-filter").select_option("s2")
    expect(cards).to_have_count(1)
    expect(cards.nth(0)).to_have_attribute("data-attention-id", "3")

    page.locator("#attention-student-filter").select_option("")
    page.locator("#attention-category-filter").select_option("learning")
    expect(cards).to_have_count(2)
    page.locator("#attention-status-filter").select_option("resolved")
    expect(cards).to_have_count(1)
    expect(cards.nth(0)).to_have_attribute("data-attention-id", "4")


def test_students_sort_by_attention_then_count_then_activity(page, static_server):
    students = [
        {"student_id": "none", "display_name": "无关注", "last_ts": 999},
        {"student_id": "medium", "display_name": "中优先",
         "open_attention_count": 9, "highest_attention_priority": "medium",
         "last_attention_at": 999},
        {"student_id": "high-old", "display_name": "高优先旧",
         "open_attention_count": 2, "highest_attention_priority": "high",
         "last_attention_at": 10},
        {"student_id": "high-new", "display_name": "高优先新",
         "open_attention_count": 2, "highest_attention_priority": "high",
         "last_attention_at": 20},
        {"student_id": "high-many", "display_name": "高优先多",
         "open_attention_count": 3, "highest_attention_priority": "high",
         "last_attention_at": 1},
    ]
    open_console(page, static_server, students=students)

    rows = page.locator(".student-item")
    expect(rows).to_have_count(5)
    assert rows.evaluate_all(
        "nodes => nodes.map(node => node.dataset.studentId)"
    ) == ["high-many", "high-new", "high-old", "medium", "none"]
    assert rows.locator(".attention-count").all_text_contents() == ["3", "2", "2", "9"]


def test_active_attention_is_not_crowded_out_by_terminal_history(page, static_server):
    terminal = [
        _attention_item(item_id, status="resolved", created_at=item_id)
        for item_id in range(1, 201)
    ]
    active = _attention_item(201, status="open", created_at=201)
    open_console(
        page,
        static_server,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        attention_items=terminal + [active],
    )

    expect(page.locator('.attention-card[data-attention-id="201"]')).to_have_count(1)
    expect(page.locator(".attention-card")).to_have_count(1)


def test_server_side_category_filter_finds_item_beyond_unfiltered_limit(
    page,
    static_server,
):
    learning = [
        _attention_item(item_id, priority="high", category="learning", created_at=item_id)
        for item_id in range(1, 201)
    ]
    system = _attention_item(
        201,
        priority="medium",
        category="system",
        created_at=201,
    )
    open_console(
        page,
        static_server,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        attention_items=learning + [system],
    )
    expect(page.locator('.attention-card[data-attention-id="201"]')).to_have_count(0)

    page.locator("#attention-category-filter").select_option("system")
    expect(page.locator('.attention-card[data-attention-id="201"]')).to_have_count(1)
    expect(page.locator(".attention-card")).to_have_count(1)


def test_attention_view_and_prefill_reuse_context_without_auto_send(
    page,
    static_server,
):
    messages = []
    suggestion = "请先运行单个失败测试，把第一条报错发给我"
    students = [
        {"student_id": "s1", "display_name": "学员甲", "last_severity": "info",
         "session_count": 1, "analysis_count": 1},
        {"student_id": "s2", "display_name": "学员乙", "last_severity": "error",
         "session_count": 1, "analysis_count": 1, "open_attention_count": 1,
         "highest_attention_priority": "high"},
    ]
    sessions = {
        "s1": [{"session_id": "sess1", "session_title": "对话甲", "analysis_count": 1}],
        "s2": [{"session_id": "sess2", "session_title": "需要干预的对话", "analysis_count": 1}],
    }
    timeline = {"sess2": [{"type": "analysis", "content": "卡点原文", "created_at": 2}]}
    items = [_attention_item(21, student_id="s2", session_id="sess2",
                             suggested_action=suggestion)]
    open_console(
        page,
        static_server,
        students=students,
        sessions_by_student=sessions,
        timeline_by_session=timeline,
        attention_items=items,
        mentor_messages=messages,
    )

    card = page.locator('.attention-card[data-attention-id="21"]')
    card.locator('[data-action="view"]').click()
    expect(page.locator('.student-item[data-student-id="s2"]')).to_have_class(
        re.compile(r"\bselected\b")
    )
    expect(page.locator('.session-item[data-session-id="sess2"]')).to_have_class(
        re.compile(r"\bselected\b")
    )
    expect(page.locator("#timeline")).to_contain_text("卡点原文")

    card.locator('[data-action="prefill"]').click()
    expect(page.locator("#compose-input")).to_have_value(suggestion)
    assert messages == []
    expect(page.locator("#timeline .card-me")).to_have_count(0)

    page.locator("#compose-send").click()
    expect(page.locator("#timeline .card-me")).to_have_count(1)
    assert len(messages) == 1
    assert messages[0]["student_id"] == "s2"
    assert messages[0]["text"] == suggestion
    assert messages[0]["client_request_id"].startswith("mentor-message-")

    # 同一学员/对话再次查看关注项，不得清空尚在等回执的导师消息。
    card.locator('[data-action="view"]').click()
    expect(page.locator("#timeline .card-me")).to_have_count(1)


def test_attention_cross_student_navigation_preserves_delivery_receipt_state(
    page,
    static_server,
):
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    students = [
        {"student_id": "a", "display_name": "A 学员"},
        {"student_id": "b", "display_name": "B 学员"},
    ]
    sessions = {
        "a": [{"session_id": "sess-a", "session_title": "A 对话"}],
        "b": [{"session_id": "sess-b", "session_title": "B 对话"}],
    }
    install_api_routes(
        page,
        students=students,
        sessions_by_student=sessions,
        timeline_by_session={"sess-a": [], "sess-b": []},
        attention_items=[
            _attention_item(23, student_id="a", session_id="sess-a", created_at=10),
            _attention_item(24, student_id="b", session_id="sess-b", created_at=20),
        ],
    )
    page.goto(static_server + "/index.html")

    page.locator('.attention-card[data-attention-id="23"] [data-action="view"]').click()
    page.locator("#compose-input").fill("给 A 的导师提示")
    page.locator("#compose-send").click()
    expect(page.locator("#timeline .card-me .pill")).to_have_text("发送中…")

    page.locator('.attention-card[data-attention-id="24"] [data-action="view"]').click()
    expect(page.locator("#timeline .card-me")).to_have_count(0)
    ws_holder["socket"].send(json.dumps({
        "type": "message_delivered",
        "student_id": "a",
        "message_id": "message-1",
        "id": 1,
    }))

    page.locator('.attention-card[data-attention-id="23"] [data-action="view"]').click()
    expect(page.locator("#timeline .card-me")).to_have_count(1)
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")


def test_delivery_receipt_before_message_post_response_is_not_lost(page, static_server):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/message') {
            return new Promise((resolve) => {
              window.__resolveMentorMessage = () => resolve(new Response(JSON.stringify({
                message_id: 'message-early', id: 99, delivered: false
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(25)],
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="25"] [data-action="view"]').click()
    page.locator("#compose-input").fill("早回执消息")
    page.locator("#compose-send").click()
    page.wait_for_function("() => typeof window.__resolveMentorMessage === 'function'")

    ws_holder["socket"].send(json.dumps({
        "type": "message_delivered",
        "student_id": "s1",
        "message_id": "message-early",
        "id": 99,
    }))
    page.evaluate("window.__resolveMentorMessage()")
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")


def test_lost_message_post_response_recovers_from_status_on_first_ws_open(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.WebSocket = class ControlledWebSocket {
          constructor() { window.__controlledMentorSocket = this; }
        };
        window.fetch = async (input, options) => {
          const url = new URL(String(input), location.href);
          const response = await realFetch(input, options);
          if (url.pathname === '/api/mentor/message' && !window.__mentorResponseLost) {
            window.__mentorResponseLost = true;
            throw new TypeError('response lost after persistence');
          }
          return response;
        };
      })();
    """)
    messages = []
    statuses = {}
    status_requests = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(26)],
        mentor_messages=messages,
        mentor_message_statuses=statuses,
        mentor_status_requests=status_requests,
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="26"] [data-action="view"]').click()
    page.locator("#compose-input").fill("响应丢失但已入库")
    with page.expect_response(
        lambda response: urlparse(response.url).path == "/api/mentor/messages/status"
    ):
        page.locator("#compose-send").click()
    page.wait_for_function("() => window.__mentorResponseLost === true")

    assert len(messages) == 1
    client_request_id = messages[0].get("client_request_id")
    assert client_request_id
    assert client_request_id in statuses
    assert any(client_request_id in request for request in status_requests)
    assert len(messages) == 1
    statuses[client_request_id]["delivered"] = True

    page.evaluate("window.__controlledMentorSocket.onopen()")
    expect(page.locator("#timeline .card-me")).to_have_count(1)
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")
    assert len(messages) == 1
    assert sum(client_request_id in request for request in status_requests) >= 2


def test_lost_response_rechecks_same_id_until_delayed_commit_without_ws_reconnect(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.WebSocket = class ControlledWebSocket {
          constructor() { window.__controlledMentorSocket = this; }
        };
        window.__messagePostAttempts = 0;
        window.__statusRequestTimes = [];
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/messages/status') {
            window.__statusRequestTimes.push(performance.now());
          }
          if (url.pathname === '/api/mentor/message') {
            window.__messagePostAttempts += 1;
            window.__delayedMessagePayload = JSON.parse(options.body);
            return Promise.reject(new TypeError('connection closed before commit'));
          }
          return realFetch(input, options);
        };
      })();
    """)
    statuses = {}
    status_requests = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(29)],
        mentor_message_statuses=statuses,
        mentor_status_requests=status_requests,
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="29"] [data-action="view"]').click()
    page.locator("#compose-input").fill("后端延迟提交")
    with page.expect_response(
        lambda response: urlparse(response.url).path == "/api/mentor/messages/status"
    ):
        page.locator("#compose-send").click()

    payload = page.evaluate("window.__delayedMessagePayload")
    client_request_id = payload["client_request_id"]
    assert status_requests == [[client_request_id]]
    assert page.evaluate("window.__messagePostAttempts") == 1

    # 超过原 1s 窗口后事务才可见（SQLite 默认 lock wait 可达 5s）；
    # 不触发 WS 重连，仍必须用同一键收敛。
    page.wait_for_timeout(1200)
    assert len(status_requests) == 3
    statuses[client_request_id] = {
        "client_request_id": client_request_id,
        "message_id": "message-delayed",
        "id": 29,
        "student_id": "s1",
        "delivered": True,
    }
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")
    assert page.evaluate("window.__messagePostAttempts") == 1
    assert len(status_requests) == 4
    assert all(request == [client_request_id] for request in status_requests)
    request_times = page.evaluate("window.__statusRequestTimes")
    assert 150 <= request_times[1] - request_times[0] <= 700
    assert 700 <= request_times[2] - request_times[0] <= 1600
    assert 2400 <= request_times[3] - request_times[0] <= 3900

    # 第四次命中即停，不应再跑 6s 补查。
    page.wait_for_timeout(3200)
    assert len(status_requests) == 4


def test_client_request_id_receipt_matches_before_post_response(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/message') {
            window.__pendingMentorPayload = JSON.parse(options.body);
            return new Promise(() => {});
          }
          return realFetch(input, options);
        };
      })();
    """)
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(33)],
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="33"] [data-action="view"]').click()
    page.locator("#compose-input").fill("只知道客户端幂等键")
    page.locator("#compose-send").click()
    page.wait_for_function("() => !!window.__pendingMentorPayload")
    client_request_id = page.evaluate(
        "window.__pendingMentorPayload.client_request_id"
    )

    ws_holder["socket"].send(json.dumps({
        "type": "message_delivered",
        "student_id": "s1",
        "message_id": "message-late",
        "id": 33,
        "client_request_id": client_request_id,
    }))
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")


def test_status_hit_undelivered_clears_false_failed_state(page, static_server):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.WebSocket = class ControlledWebSocket {
          constructor() { window.__controlledMentorSocket = this; }
        };
        window.fetch = async (input, options) => {
          const url = new URL(String(input), location.href);
          const response = await realFetch(input, options);
          if (url.pathname === '/api/mentor/message') {
            throw new TypeError('response lost after persistence');
          }
          return response;
        };
      })();
    """)
    messages = []
    statuses = {}
    status_requests = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(30)],
        mentor_messages=messages,
        mentor_message_statuses=statuses,
        mentor_status_requests=status_requests,
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="30"] [data-action="view"]').click()
    page.locator("#compose-input").fill("已入库但未送达")
    with page.expect_response(
        lambda response: urlparse(response.url).path == "/api/mentor/messages/status"
    ):
        page.locator("#compose-send").click()

    expect(page.locator("#timeline .card-me .pill")).to_have_text("发送中…")
    assert len(messages) == 1
    assert status_requests == [[messages[0]["client_request_id"]]]


def test_stale_undelivered_status_cannot_downgrade_newer_ws_receipt(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.WebSocket = class ControlledWebSocket {
          constructor() { window.__controlledMentorSocket = this; }
        };
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/messages/status' && window.__holdMessageStatus) {
            const clientRequestId = JSON.parse(options.body).client_request_ids[0];
            return new Promise((resolve) => {
              window.__resolveOldMessageStatus = () => resolve(new Response(JSON.stringify({
                items: [{
                  client_request_id: clientRequestId,
                  message_id: 'message-1', id: 1, student_id: 's1', delivered: false
                }]
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    messages = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(32)],
        mentor_messages=messages,
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="32"] [data-action="view"]').click()
    page.evaluate("() => sendMentorMessage('新回执不得被旧快照覆盖')")
    assert len(messages) == 1

    page.evaluate("window.__holdMessageStatus = true")
    page.evaluate("window.__controlledMentorSocket.onopen()")
    page.wait_for_function("() => typeof window.__resolveOldMessageStatus === 'function'")
    page.evaluate("""
      window.__controlledMentorSocket.onmessage({data: JSON.stringify({
        type: 'message_delivered', student_id: 's1', message_id: 'message-1', id: 1
      })})
    """)
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")

    page.evaluate("window.__resolveOldMessageStatus()")
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")


def test_ws_reconnect_recovers_delivery_ack_missed_while_disconnected(
    page,
    static_server,
):
    page.add_init_script("""
      window.WebSocket = class ControlledWebSocket {
        constructor() { window.__controlledMentorSocket = this; }
      };
    """)
    messages = []
    statuses = {}
    status_requests = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(27)],
        mentor_messages=messages,
        mentor_message_statuses=statuses,
        mentor_status_requests=status_requests,
    )
    page.goto(static_server + "/index.html")
    page.evaluate("window.__controlledMentorSocket.onopen()")
    page.locator('.attention-card[data-attention-id="27"] [data-action="view"]').click()
    page.evaluate("() => sendMentorMessage('断线期间学员已确认')")

    assert len(messages) == 1
    client_request_id = messages[0].get("client_request_id")
    assert client_request_id
    statuses[client_request_id]["delivered"] = True
    expect(page.locator("#timeline .card-me .pill")).to_have_text("发送中…")

    # 用同一 onopen 回调模拟重连成功：不自动重发，只查状态。
    page.evaluate("window.__controlledMentorSocket.onopen()")
    expect(page.locator("#timeline .card-me")).to_have_count(1)
    expect(page.locator("#timeline .card-me .pill")).to_have_text("✓ 已展示")
    assert len(messages) == 1
    assert any(client_request_id in request for request in status_requests)


def test_outbound_message_recovery_is_bounded_to_300_without_duplicate_render(
    page,
    static_server,
):
    page.add_init_script("""
      window.WebSocket = class ControlledWebSocket {
        constructor() { window.__controlledMentorSocket = this; }
      };
    """)
    messages = []
    statuses = {}
    status_requests = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={
            "s1": [{"session_id": "sess1", "session_title": "对话"}],
        },
        timeline_by_session={"sess1": []},
        attention_items=[_attention_item(28)],
        mentor_messages=messages,
        mentor_message_statuses=statuses,
        mentor_status_requests=status_requests,
    )
    page.goto(static_server + "/index.html")
    page.locator('.attention-card[data-attention-id="28"] [data-action="view"]').click()
    page.evaluate("""
      () => Promise.all(Array.from({length: 301}, (_, index) =>
        sendMentorMessage('批量消息 ' + index)
      ))
    """)

    assert len(messages) == 301
    client_request_ids = [message.get("client_request_id") for message in messages]
    assert all(client_request_ids)
    assert len(set(client_request_ids)) == 301
    expect(page.locator("#timeline .card-me")).to_have_count(300)

    page.evaluate("window.__controlledMentorSocket.onopen()")
    expect(page.locator("#timeline .card-me")).to_have_count(300)
    assert status_requests
    assert len(status_requests[-1]) == 300
    assert len(set(status_requests[-1])) == 300
    assert client_request_ids[0] not in status_requests[-1]
    assert client_request_ids[-1] in status_requests[-1]
    assert len(messages) == 301


def test_attention_status_actions_patch_and_update_active_student_count(
    page,
    static_server,
):
    patches = []
    students = [{
        "student_id": "s1",
        "display_name": "学员甲",
        "last_severity": "error",
        "session_count": 1,
        "analysis_count": 1,
        "open_attention_count": 1,
        "highest_attention_priority": "high",
    }]
    items = [_attention_item(31)]
    open_console(
        page,
        static_server,
        students=students,
        attention_items=items,
        attention_patches=patches,
    )

    card = page.locator('.attention-card[data-attention-id="31"]')
    expect(page.locator('.student-item .attention-count')).to_have_text("1")
    card.locator('[data-action="in_progress"]').click()
    expect(card.locator(".attention-status")).to_have_text("处理中")
    assert patches[-1]["status"] == "in_progress"
    assert patches[-1]["method"] == "PATCH"
    assert patches[-1]["mentor_id"].startswith("mentor-console-")
    assert page.evaluate(
        "localStorage.getItem('workbuddy_copilot_mentor_id')"
    ) == patches[-1]["mentor_id"]

    card.locator('[data-action="resolved"]').click()
    expect(card).to_have_count(0)  # 默认 active 中已解决项立即移出
    assert patches[-1]["status"] == "resolved"
    expect(page.locator('.student-item .attention-count')).to_have_count(0)


def test_patch_websocket_before_http_response_decrements_student_count_once(
    page,
    static_server,
):
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )

    def publish_before_response(item):
        ws_holder["socket"].send(json.dumps({
            "type": "attention_updated",
            "action": "updated",
            "item": {key: value for key, value in item.items()
                     if key != "resolution_note"},
        }))

    install_api_routes(
        page,
        students=[{
            "student_id": "s1", "display_name": "学员甲",
            "open_attention_count": 3, "highest_attention_priority": "high",
        }],
        attention_items=[_attention_item(34)],
        attention_patch_hook=publish_before_response,
    )
    page.goto(static_server + "/index.html")

    page.locator('.attention-card[data-attention-id="34"] [data-action="resolved"]').click()
    expect(page.locator('.student-item .attention-count')).to_have_text("2")


def test_resolving_last_high_item_downgrades_student_priority_sort(
    page,
    static_server,
):
    students = [
        {"student_id": "s1", "display_name": "原高优先",
         "open_attention_count": 2, "highest_attention_priority": "high",
         "last_attention_at": 10},
        {"student_id": "s2", "display_name": "中优先更新",
         "open_attention_count": 1, "highest_attention_priority": "medium",
         "last_attention_at": 100},
    ]
    items = [
        _attention_item(35, student_id="s1", priority="high", created_at=10),
        _attention_item(36, student_id="s1", priority="medium", created_at=11),
        _attention_item(37, student_id="s2", priority="medium", created_at=100),
    ]
    open_console(page, static_server, students=students, attention_items=items)
    expect(page.locator(".student-item").first).to_have_attribute("data-student-id", "s1")

    page.locator('.attention-card[data-attention-id="35"] [data-action="resolved"]').click()
    expect(page.locator(".student-item").first).to_have_attribute("data-student-id", "s2")


def test_attention_dismissed_action_removes_active_card(page, static_server):
    patches = []
    students = [{
        "student_id": "s1", "display_name": "学员甲", "last_severity": "warn",
        "open_attention_count": 1, "highest_attention_priority": "medium",
    }]
    open_console(
        page,
        static_server,
        students=students,
        attention_items=[_attention_item(32, priority="medium")],
        attention_patches=patches,
    )

    page.locator('.attention-card[data-attention-id="32"] [data-action="dismissed"]').click()
    expect(page.locator('.attention-card[data-attention-id="32"]')).to_have_count(0)
    assert patches[-1]["status"] == "dismissed"
    assert patches[-1]["mentor_id"].startswith("mentor-console-")


def test_attention_patch_failure_keeps_authoritative_status_and_shows_error(
    page,
    static_server,
):
    open_console(
        page,
        static_server,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        attention_items=[_attention_item(33)],
        attention_patch_status=409,
    )

    card = page.locator('.attention-card[data-attention-id="33"]')
    card.locator('[data-action="resolved"]').click()
    expect(card).to_have_count(1)
    expect(card.locator(".attention-status")).to_have_text("未处理")
    expect(card.locator(".attention-error")).to_have_text("更新失败，请重试")

    # 后续权威 REST 快照已证明真实状态，不得永久保留旧的 UI 错误。
    page.locator("#attention-refresh").click()
    expect(card.locator(".attention-error")).to_have_count(0)


def test_attention_websocket_increment_is_sorted_and_terminal_update_removes_card(
    page,
    static_server,
):
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲",
                   "last_severity": "info", "session_count": 1, "analysis_count": 1}],
        attention_items=[_attention_item(41, priority="medium", created_at=1)],
    )
    page.goto(static_server + "/index.html")
    expect(page.locator(".attention-card")).to_have_count(1)

    created = _attention_item(42, priority="high", created_at=20)
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "created",
        "item": created,
    }))
    expect(page.locator(".attention-card")).to_have_count(2)
    assert page.locator(".attention-card").evaluate_all(
        "nodes => nodes.map(node => Number(node.dataset.attentionId))"
    ) == [42, 41]

    created["status"] = "resolved"
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "updated",
        "item": created,
    }))
    expect(page.locator(".attention-card")).to_have_count(1)
    expect(page.locator(".attention-card")).to_have_attribute("data-attention-id", "41")


def test_attention_websocket_item_remains_visible_after_rest_load_error(
    page,
    static_server,
):
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        attention_status=500,
    )
    page.goto(static_server + "/index.html")
    expect(page.locator(".attention-error")).to_have_text("关注队列加载失败")

    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "created",
        "item": _attention_item(45),
    }))
    expect(page.locator('.attention-card[data-attention-id="45"]')).to_have_count(1)
    expect(page.locator(".attention-error")).to_have_text("关注队列加载失败")


def test_attention_missing_source_session_reports_fallback_to_student(
    page,
    static_server,
):
    open_console(
        page,
        static_server,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        sessions_by_student={"s1": []},
        attention_items=[_attention_item(46, session_id="missing-session")],
    )

    page.locator('.attention-card[data-attention-id="46"] [data-action="view"]').click()
    expect(page.locator('.student-item[data-student-id="s1"]')).to_have_class(
        re.compile(r"\bselected\b")
    )
    expect(page.locator("#attention-feedback")).to_have_text(
        "来源对话当前不可用，已定位到学员"
    )


def test_attention_duplicate_and_stale_websocket_events_do_not_regress_state(
    page,
    static_server,
):
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    authoritative_items = []
    students = [{"student_id": "s1", "display_name": "学员甲",
                 "open_attention_count": 0}]
    install_api_routes(
        page,
        students=students,
        attention_items=authoritative_items,
    )
    page.goto(static_server + "/index.html")
    expect(page.locator(".attention-empty")).to_be_visible()

    created = _attention_item(43, created_at=20)
    event = {"type": "attention_updated", "action": "created", "item": created}
    authoritative_items.append(dict(created))
    students[0].update({"open_attention_count": 1, "highest_attention_priority": "high"})
    ws_holder["socket"].send(json.dumps(event))
    ws_holder["socket"].send(json.dumps(event))
    expect(page.locator('.attention-card[data-attention-id="43"]')).to_have_count(1)
    expect(page.locator('.student-item .attention-count')).to_have_text("1")

    resolved = dict(created, status="resolved", updated_at=30)
    authoritative_items[0].update(resolved)
    students[0].update({"open_attention_count": 0, "highest_attention_priority": None})
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated", "action": "updated", "item": resolved,
    }))
    expect(page.locator('.attention-card[data-attention-id="43"]')).to_have_count(0)
    expect(page.locator('.student-item .attention-count')).to_have_count(0)

    stale_open = dict(created, status="open", updated_at=20)
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated", "action": "updated", "item": stale_open,
    }))
    page.locator("#attention-status-filter").select_option("")
    card = page.locator('.attention-card[data-attention-id="43"]')
    expect(card).to_have_count(1)
    expect(card.locator(".attention-status")).to_have_text("已解决")


def test_delayed_created_ws_cannot_double_count_authoritative_student_snapshot(
    page,
    static_server,
):
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    # 模拟该项在 attention 的有界/筛选缓存外，但 students 权威聚合已计入。
    students = [{
        "student_id": "s1", "display_name": "学员甲",
        "open_attention_count": 1, "highest_attention_priority": "high",
    }]
    install_api_routes(
        page,
        students=students,
        attention_items=[],
    )
    page.goto(static_server + "/index.html")
    expect(page.locator('.student-item .attention-count')).to_have_text("1")

    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "created",
        "item": _attention_item(56, created_at=90),
    }))
    expect(page.locator('.attention-card[data-attention-id="56"]')).to_have_count(1)
    page.wait_for_timeout(200)
    expect(page.locator('.student-item .attention-count')).to_have_text("1")


def test_attention_websocket_before_initial_get_is_not_lost_or_regressed(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.__attentionResolvers = [];
        window.fetch = (input, options) => {
          const url = String(input);
          if (url.includes('/api/mentor/attention')) {
            return new Promise((resolve) => {
              window.__attentionResolvers.push(resolve);
              window.__resolveAttention = () => {
                const payload = JSON.stringify({items: [{
                  id: 44, source_type: 'analysis', source_id: '44', category: 'learning',
                  student_id: 's1', session_id: 'sess1', priority: 'medium',
                  reason_code: 'analysis_warn', reason: '过时原因', evidence: [],
                  suggested_action: '过时建议', confidence: 0.6, status: 'open',
                  created_at: 10, updated_at: 10
                }]});
                window.__attentionResolvers.splice(0).forEach((done) => done(new Response(
                  payload, {status: 200, headers: {'Content-Type': 'application/json'}}
                )));
              };
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
    )
    page.goto(static_server + "/index.html", wait_until="domcontentloaded")
    page.wait_for_function("() => typeof window.__resolveAttention === 'function'")

    fresh = _attention_item(44, priority="high", reason="实时新原因", created_at=30)
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated", "action": "created", "item": fresh,
    }))
    expect(page.locator('.attention-card[data-attention-id="44"] .attention-reason')).to_have_text(
        "实时新原因"
    )
    page.evaluate("window.__resolveAttention()")
    expect(page.locator('.attention-card[data-attention-id="44"] .attention-reason')).to_have_text(
        "实时新原因"
    )


def test_first_websocket_open_closes_rest_handoff_gap(page, static_server):
    page.add_init_script("""
      window.WebSocket = class ControlledWebSocket {
        constructor() { window.__controlledMentorSocket = this; }
      };
    """)
    authoritative_items = []
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲"}],
        attention_items=authoritative_items,
    )
    page.goto(static_server + "/index.html")
    expect(page.locator(".attention-empty")).to_be_visible()

    authoritative_items.append(_attention_item(55, created_at=30))
    page.evaluate("window.__controlledMentorSocket.onopen()")
    expect(page.locator('.attention-card[data-attention-id="55"]')).to_have_count(1)


def test_authoritative_refresh_removes_untouched_ghost_but_replays_new_ws_item(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.__refreshAttentionResolvers = [];
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/attention' && window.__holdAttentionRefresh) {
            return new Promise((resolve) => {
              window.__refreshAttentionResolvers.push(resolve);
              window.__resolveRefreshAttention = () => {
                window.__holdAttentionRefresh = false;
                window.__refreshAttentionResolvers.splice(0).forEach((done) => done(
                  new Response(JSON.stringify({items: []}), {
                    status: 200, headers: {'Content-Type': 'application/json'}
                  })
                ));
              };
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    authoritative_items = [_attention_item(51, created_at=10)]
    install_api_routes(
        page,
        students=[{"student_id": "s1", "display_name": "学员甲",
                   "open_attention_count": 1, "highest_attention_priority": "high"}],
        attention_items=authoritative_items,
    )
    page.goto(static_server + "/index.html")
    expect(page.locator('.attention-card[data-attention-id="51"]')).to_have_count(1)

    authoritative_items.clear()
    page.evaluate("window.__holdAttentionRefresh = true")
    page.locator("#attention-refresh").click()
    page.wait_for_function("() => window.__refreshAttentionResolvers.length === 2")
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "created",
        "item": _attention_item(52, created_at=20),
    }))
    expect(page.locator(".attention-card")).to_have_count(2)
    page.evaluate("window.__resolveRefreshAttention()")

    expect(page.locator('.attention-card[data-attention-id="51"]')).to_have_count(0)
    expect(page.locator('.attention-card[data-attention-id="52"]')).to_have_count(1)


def test_slow_old_student_sessions_cannot_replace_new_selection(page, static_server):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = String(input);
          if (url.includes('/api/mentor/students/a/sessions')) {
            return new Promise((resolve) => {
              window.__resolveOldSessions = () => resolve(new Response(JSON.stringify({
                items: [{session_id: 'old-a', session_title: '过时 A 对话'}]
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    open_console(
        page,
        static_server,
        students=[
            {"student_id": "a", "display_name": "A"},
            {"student_id": "b", "display_name": "B"},
        ],
        sessions_by_student={
            "b": [{"session_id": "b-current", "session_title": "B 对话"}],
        },
    )
    page.locator('.student-item[data-student-id="a"]').click()
    page.wait_for_function("() => typeof window.__resolveOldSessions === 'function'")
    page.locator('.student-item[data-student-id="b"]').click()
    expect(page.locator('.student-item[data-student-id="b"]')).to_have_class(
        re.compile(r"\bselected\b")
    )
    expect(page.locator('.session-item[data-session-id="b-current"]')).to_have_count(1)

    page.evaluate("window.__resolveOldSessions()")
    page.wait_for_timeout(100)
    expect(page.locator('.session-item[data-session-id="b-current"]')).to_have_count(1)
    expect(page.locator('.session-item[data-session-id="old-a"]')).to_have_count(0)


@pytest.mark.parametrize(
    ("attention_status", "expected_selector", "expected_text"),
    [
        (200, ".attention-empty", "暂无需要关注的学员"),
        (500, ".attention-error", "关注队列加载失败"),
    ],
)
def test_attention_empty_and_error_states(
    page,
    static_server,
    attention_status,
    expected_selector,
    expected_text,
):
    open_console(
        page,
        static_server,
        attention_items=[],
        attention_status=attention_status,
    )

    expect(page.locator(expected_selector)).to_have_text(expected_text)


def test_attention_loading_state_is_visible_while_request_is_pending(page, static_server):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.__attentionResolvers = [];
        window.fetch = (input, options) => {
          if (String(input).includes('/api/mentor/attention')) {
            return new Promise((resolve) => {
              window.__attentionResolvers.push(resolve);
              window.__resolveAttention = () => {
                window.__attentionResolvers.splice(0).forEach((done) => done(new Response(
                  JSON.stringify({items: []}),
                  {status: 200, headers: {'Content-Type': 'application/json'}}
                )));
              };
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    install_ws_route(page)
    install_api_routes(page, attention_items=[])

    page.goto(static_server + "/index.html", wait_until="domcontentloaded")

    expect(page.locator(".attention-loading")).to_be_visible()
    page.evaluate("window.__resolveAttention()")
    expect(page.locator(".attention-empty")).to_have_text("暂无需要关注的学员")


def test_attention_refresh_reloads_queue_and_student_aggregates(page, static_server):
    students = [{
        "student_id": "s1", "display_name": "学员甲",
        "open_attention_count": 0, "highest_attention_priority": None,
    }]
    items = []
    open_console(
        page,
        static_server,
        students=students,
        attention_items=items,
    )
    expect(page.locator(".attention-empty")).to_be_visible()

    students[0].update({
        "open_attention_count": 1,
        "highest_attention_priority": "high",
        "last_attention_at": 50,
    })
    items.append(_attention_item(47, created_at=50))
    page.locator("#attention-refresh").click()

    expect(page.locator('.attention-card[data-attention-id="47"]')).to_have_count(1)
    expect(page.locator('.student-item[data-student-id="s1"] .attention-count')).to_have_text("1")


def test_refresh_stale_students_response_cannot_overwrite_new_attention_event(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/students' && window.__holdNextStudents) {
            window.__holdNextStudents = false;
            return new Promise((resolve) => {
              window.__resolveStaleStudents = () => resolve(new Response(JSON.stringify({
                items: [{
                  student_id: 's1', display_name: '学员甲',
                  open_attention_count: 0, highest_attention_priority: null
                }]
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    students = [{
        "student_id": "s1", "display_name": "学员甲",
        "open_attention_count": 0, "highest_attention_priority": None,
    }]
    install_api_routes(
        page,
        students=students,
        attention_items=[],
    )
    page.goto(static_server + "/index.html")
    page.evaluate("window.__holdNextStudents = true")
    page.locator("#attention-refresh").click()
    page.wait_for_function("() => typeof window.__resolveStaleStudents === 'function'")

    students[0].update({
        "open_attention_count": 1, "highest_attention_priority": "high",
    })
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "created",
        "item": _attention_item(48, created_at=60),
    }))
    expect(page.locator('.student-item .attention-count')).to_have_text("1")
    page.evaluate("window.__resolveStaleStudents()")
    expect(page.locator('.student-item .attention-count')).to_have_text("1")


def test_unrelated_student_event_does_not_block_authoritative_aggregate_correction(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/students' && window.__holdNextStudents) {
            window.__holdNextStudents = false;
            return new Promise((resolve) => {
              window.__resolveCorrectedStudents = () => resolve(new Response(JSON.stringify({
                items: [
                  {student_id: 'a', display_name: 'A', open_attention_count: 0,
                   highest_attention_priority: null},
                  {student_id: 'b', display_name: 'B', open_attention_count: 0,
                   highest_attention_priority: null}
                ]
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    ws_holder = {}
    page.route_web_socket(
        "**/ws/mentor",
        lambda ws: ws_holder.setdefault("socket", ws),
    )
    authoritative_items = [
        _attention_item(53, student_id="a", session_id="sess-a", created_at=10),
    ]
    students = [
        {"student_id": "a", "display_name": "A", "open_attention_count": 1,
         "highest_attention_priority": "high"},
        {"student_id": "b", "display_name": "B", "open_attention_count": 0,
         "highest_attention_priority": None},
    ]
    install_api_routes(
        page,
        students=students,
        attention_items=authoritative_items,
    )
    page.goto(static_server + "/index.html")
    expect(page.locator('.student-item[data-student-id="a"] .attention-count')).to_have_text("1")

    authoritative_items.clear()
    students[0].update({"open_attention_count": 0, "highest_attention_priority": None})
    page.evaluate("window.__holdNextStudents = true")
    page.locator("#attention-refresh").click()
    page.wait_for_function("() => typeof window.__resolveCorrectedStudents === 'function'")
    students[1].update({"open_attention_count": 1, "highest_attention_priority": "high"})
    ws_holder["socket"].send(json.dumps({
        "type": "attention_updated",
        "action": "created",
        "item": _attention_item(54, student_id="b", session_id="sess-b", created_at=20),
    }))
    page.evaluate("window.__resolveCorrectedStudents()")

    expect(page.locator('.student-item[data-student-id="a"] .attention-count')).to_have_count(0)
    expect(page.locator('.student-item[data-student-id="b"] .attention-count')).to_have_text("1")


def test_older_students_refresh_cannot_overwrite_newer_authoritative_response(
    page,
    static_server,
):
    page.add_init_script("""
      (() => {
        const realFetch = window.fetch.bind(window);
        window.fetch = (input, options) => {
          const url = new URL(String(input), location.href);
          if (url.pathname === '/api/mentor/students' && window.__holdNextStudents) {
            window.__holdNextStudents = false;
            return new Promise((resolve) => {
              window.__resolveOlderStudents = () => resolve(new Response(JSON.stringify({
                items: [{
                  student_id: 's1', display_name: '学员甲',
                  open_attention_count: 0, highest_attention_priority: null
                }]
              }), {status: 200, headers: {'Content-Type': 'application/json'}}));
            });
          }
          return realFetch(input, options);
        };
      })();
    """)
    students = [{
        "student_id": "s1", "display_name": "学员甲",
        "open_attention_count": 0, "highest_attention_priority": None,
    }]
    open_console(page, static_server, students=students, attention_items=[])

    page.evaluate("window.__holdNextStudents = true")
    page.locator("#attention-refresh").click()
    page.wait_for_function("() => typeof window.__resolveOlderStudents === 'function'")
    page.wait_for_function("() => !document.querySelector('#attention-refresh').disabled")
    students[0].update({
        "open_attention_count": 2,
        "highest_attention_priority": "high",
        "last_attention_at": 80,
    })
    page.locator("#attention-refresh").click()
    expect(page.locator('.student-item .attention-count')).to_have_text("2")

    page.evaluate("window.__resolveOlderStudents()")
    page.wait_for_timeout(100)
    expect(page.locator('.student-item .attention-count')).to_have_text("2")


def test_concurrent_refresh_401_uses_single_shared_token_prompt(page, static_server):
    page.add_init_script("""
      sessionStorage.setItem('workbuddy_copilot_mentor_token', 'old-token');
      window.__mentorPromptCount = 0;
      window.prompt = () => {
        window.__mentorPromptCount += 1;
        return 'new-token-' + window.__mentorPromptCount;
      };
    """)
    install_ws_route(page)

    def auth_handler(route):
        request = route.request
        auth = request.headers.get("authorization", "")
        if auth == "Bearer old-token":
            route.fulfill(status=401, json={"detail": "expired"})
            return
        path = urlparse(request.url).path
        if path == "/api/mentor/students":
            route.fulfill(json={"items": []})
        elif path == "/api/mentor/attention":
            route.fulfill(json={"items": []})
        else:
            route.fulfill(status=404, json={"detail": "not found"})

    page.route("**/api/mentor/**", auth_handler)
    page.goto(static_server + "/index.html")
    expect(page.locator(".attention-empty")).to_be_visible()
    assert page.evaluate("window.__mentorPromptCount") == 1

    page.evaluate(
        "sessionStorage.setItem('workbuddy_copilot_mentor_token', 'old-token')"
    )
    page.locator("#attention-refresh").click()
    page.wait_for_function("() => !document.querySelector('#attention-refresh').disabled")
    assert page.evaluate("window.__mentorPromptCount") == 2
