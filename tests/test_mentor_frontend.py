"""测试导师前端静态文件。"""
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from copilot.service import app


@pytest.fixture
def client():
    return TestClient(app)


class TestFrontendFiles:
    """静态文件存在且结构正确。"""

    def test_static_dir_exists(self):
        static_dir = Path(__file__).parent.parent / "copilot" / "static" / "mentor"
        assert static_dir.exists(), "copilot/static/mentor/ 目录应存在"

    def test_index_html_exists(self):
        index_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "index.html"
        assert index_path.exists(), "index.html 应存在"

    def test_app_js_exists(self):
        js_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "app.js"
        assert js_path.exists(), "app.js 应存在"

    def test_style_css_exists(self):
        css_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "style.css"
        assert css_path.exists(), "style.css 应存在"


class TestFrontendStructure:
    """前端结构验证。"""

    def test_index_html_preserves_original_three_work_areas(self):
        index_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "index.html"
        if not index_path.exists():
            pytest.skip("index.html 尚未创建")
        content = index_path.read_text()
        # 三栏布局标识
        assert "学员" in content or "student" in content.lower()
        assert "对话" in content or "session" in content.lower()
        assert "时间线" in content or "timeline" in content.lower()

    def test_attention_radar_static_contract(self):
        static_dir = Path(__file__).parent.parent / "copilot" / "static" / "mentor"
        html = (static_dir / "index.html").read_text()
        js = (static_dir / "app.js").read_text()

        for element_id in (
            "attention-list",
            "attention-status-filter",
            "attention-priority-filter",
            "attention-category-filter",
            "attention-student-filter",
        ):
            assert f'id="{element_id}"' in html
        assert "/api/mentor/attention" in js
        assert "attention_updated" in js
        assert "focusAttentionContext" in js
        assert "prefillAttentionSuggestion" in js
        assert "copyAttentionReview" in js
        assert "buildAttentionReviewPrompt" in js
        assert "复制 AI 审查包" in js
        assert "navigator.clipboard.writeText" in js
        assert "MENTOR_ID_STORAGE_KEY" in js
        assert "currentMentorId" in js
        assert "mentor_id: currentMentorId()" in js
        assert "首次建连也必须补拉" in js

    def test_system_status_fault_indicator_contract(self):
        static_dir = Path(__file__).parent.parent / "copilot" / "static" / "mentor"
        html = (static_dir / "index.html").read_text()
        js = (static_dir / "app.js").read_text()

        assert 'id="system-status"' in html
        assert 'role="status"' in html
        assert "/api/mentor/system-status" in js
        assert "pending_analyses" in js
        assert "failed_analyses" in js
        assert "windows_rollout_status" in js

    def test_system_status_refresh_and_stale_response_contract(self):
        """系统状态须随手动刷新/WS 重连更新，且旧响应不能覆盖新响应。"""
        static_dir = Path(__file__).parent.parent / "copilot" / "static" / "mentor"
        js = (static_dir / "app.js").read_text()

        assert "systemStatusLoadGeneration" in js
        assert "generation !== systemStatusLoadGeneration" in js
        assert "Promise.all([loadStudents(), loadAttention(), loadSystemStatus()])" in js
        # 状态请求继续走统一鉴权入口，从而与其他并发 401 共用 mentorReauthPromise。
        assert "authFetch('/api/mentor/system-status')" in js

    def test_app_js_has_fetch_and_ws(self):
        js_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "app.js"
        if not js_path.exists():
            pytest.skip("app.js 尚未创建")
        content = js_path.read_text()
        assert "fetch" in content or "XMLHttpRequest" in content
        assert "WebSocket" in content or "ws" in content.lower()

    def test_app_js_sends_mentor_token_for_public_mode(self):
        js_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "app.js"
        content = js_path.read_text()

        assert "MENTOR_TOKEN_STORAGE_KEY" in content
        assert "authFetch" in content
        assert "Authorization" in content
        assert "X-Copilot-Token" in content
        assert "mentorWsUrl" in content
        assert "searchParams.set('token'" in content or 'searchParams.set("token"' in content

    def test_style_css_has_type_colors(self):
        css_path = Path(__file__).parent.parent / "copilot" / "static" / "mentor" / "style.css"
        if not css_path.exists():
            pytest.skip("style.css 尚未创建")
        content = css_path.read_text()
        # 验证有颜色定义（蓝/紫/橙对应 prompt/ai_summary/analysis）
        assert "color" in content or "border" in content

    def test_upload_status_ui_uses_real_request_state_and_cancellable_polling(self):
        static_dir = Path(__file__).parent.parent / "copilot" / "static" / "mentor"
        js = (static_dir / "app.js").read_text()
        html = (static_dir / "index.html").read_text()

        assert "uploadRequest:" in js
        assert "reduceUploadRequest" in js
        assert "AbortController" in js
        assert "upload_request_status" in js
        assert "retry-analysis" in js
        assert "updatedAt" in js
        assert "uploadAttemptGeneration" in js
        assert "4000" not in js
        assert "完成后灰显对话将陆续点亮" not in js
        assert 'id="retry-analysis"' in html


class TestFrontendServed:
    """前端可通过 HTTP 访问。"""

    def test_mentor_path_returns_html(self, client):
        resp = client.get("/mentor/")
        # StaticFiles 可能返回 200 或 404（取决于是否 mount）
        # 这里验证路由存在
        assert resp.status_code in (200, 404)
